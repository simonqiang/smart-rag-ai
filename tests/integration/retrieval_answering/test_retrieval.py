"""Task 13: hybrid retrieval channels, fusion, filters, and thresholds."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from ingestion.extraction import SourceObject, extract
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from retrieval_answering.evaluation import EvaluationCase, EvaluationResult, evaluate
from retrieval_answering.retrieval import (
    CONFIDENCE_THRESHOLD,
    EmptyQueryError,
    retrieve,
)

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)


def _run(db, coro_factory, store_root=None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        from foundation.storage import ObjectStore

        store = ObjectStore(store_root) if store_root is not None else None
        try:
            return await coro_factory(engine, uow, store)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _retrieve(db, workspace: dict, context: AccessContext, query: str, **kwargs) -> object:
    async def run(engine, uow, _store) -> object:
        return await retrieve(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            context=context, query=query, profile=TEST_PROFILE, **kwargs,
        )

    return _run(db, run)


def _chunk_id(db, fragment: str) -> str:
    async def run() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return str((
                    await connection.execute(
                        text("SELECT id FROM index_chunks WHERE text LIKE :fragment"),
                        {"fragment": f"%{fragment}%"},
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_exact_english_question_ranks_its_passage_first(db, workspace) -> None:
    ranked = _retrieve(db, workspace, _owner(workspace), workspace["texts"]["en"])

    assert ranked.query_language == "en"
    assert ranked.items
    assert ranked.items[0].text.startswith("Smart RAG keeps")
    assert ranked.items[0].rank == 1
    assert ranked.items[0].semantic_rank == 1
    # The chunk was hit by both channels: the keyword vector matched too.
    assert ranked.items[0].keyword_rank == 1
    assert ranked.items[0].score >= 2 * 1 / 61


def test_cross_language_query_finds_chinese_source_semantically(db, workspace) -> None:
    ranked = _retrieve(db, workspace, _owner(workspace), workspace["texts"]["zh-Hans"])

    assert ranked.query_language == "zh-Hans"
    assert ranked.items[0].language == "zh-Hans"
    assert ranked.items[0].semantic_rank == 1


def test_chinese_and_malay_keyword_hits_come_from_the_keyword_channel(
    db, workspace,
) -> None:
    # A character bigram unique to the Chinese passage hits the simple-config
    # keyword vector; the Malay content word hits the Malay passage's.
    chinese = _retrieve(db, workspace, _owner(workspace), "知识问答")
    assert any(item.keyword_rank is not None and item.language == "zh-Hans"
               for item in chinese.items)

    malay = _retrieve(db, workspace, _owner(workspace), "perlindungan maklumat")
    assert any(item.keyword_rank is not None and item.language == "ms"
               for item in malay.items)


def test_channels_dedupe_into_one_evidence_item(db, workspace) -> None:
    ranked = _retrieve(db, workspace, _owner(workspace), workspace["texts"]["en"])
    top_ids = [item.chunk_id for item in ranked.items]

    assert len(top_ids) == len(set(top_ids))
    top = ranked.items[0]
    assert (top.semantic_rank, top.keyword_rank) == (1, 1)


def test_source_filter_scopes_results_to_one_source(db, workspace) -> None:
    malay_source = workspace["corpus"]["ms"]["source_id"]
    ranked = _retrieve(
        db, workspace, _owner(workspace), workspace["texts"]["ms"],
        source_id=malay_source,
    )

    assert ranked.items
    assert {item.source_id for item in ranked.items} == {malay_source}


def test_empty_query_is_refused(db, workspace) -> None:
    with pytest.raises(EmptyQueryError):
        _retrieve(db, workspace, _owner(workspace), "   \t ")


def test_weak_query_stays_unconfident(db, workspace) -> None:
    strong = _retrieve(db, workspace, _owner(workspace), workspace["texts"]["en"])
    assert strong.is_confident

    weak = _retrieve(db, workspace, _owner(workspace), "zz qq xx yy")
    assert not weak.is_confident
    assert all(item.score < CONFIDENCE_THRESHOLD for item in weak.items)


def test_no_grants_returns_empty_evidence(db, workspace) -> None:
    stranger = AccessContext(
        user_id=str(workspace["owner_id"]),
        workspace_id=workspace["workspace_id"],
        role="member",
        collection_ids=[],
    )
    ranked = _retrieve(db, workspace, stranger, workspace["texts"]["en"])

    assert ranked.items == []


def _owner(workspace: dict) -> AccessContext:
    return workspace["owner_context"]


def _member(workspace: dict) -> AccessContext:
    return workspace["member_context"]


def test_replacement_activating_a_new_generation_excludes_the_old_chunks(
    db, workspace, store_root,
) -> None:
    old_text = workspace["texts"]["en"]
    old_chunk = _chunk_id(db, "grounded in cited evidence")

    async def rebuild(engine, uow, store) -> None:
        context = _owner(workspace)
        version_id = workspace["corpus"]["en"]["source_version_id"]
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
                {"id": version_id},
            )
            sha256 = (
                await transaction.execute(
                    text("SELECT object_sha256 FROM source_versions WHERE id = :id"),
                    {"id": version_id},
                )
            ).scalar_one()
        document = extract(SourceObject(
            source_version_id=version_id,
            media_type="text/plain",
            filename="handbook-en.txt",
            data=store.get(str(sha256)),
        ))
        staged = await index_version(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            workspace_id=context.workspace_id,
            source_version_id=version_id,
            document=document,
            profile=TEST_PROFILE,
        )
        await activate_generation(
            uow, EventWriter(), staged.generation_id,
            workspace_id=context.workspace_id,
        )

    _run(db, rebuild, store_root)

    ranked = _retrieve(db, workspace, _owner(workspace), old_text)
    assert all(item.chunk_id != old_chunk for item in ranked.items)


def test_per_language_evaluation_thresholds_pass(db, workspace) -> None:
    en_chunk = _chunk_id(db, "grounded in cited evidence")
    zh_hans_chunk = _chunk_id(db, "智能检索助手")
    zh_hant_chunk = _chunk_id(db, "員工學習")
    ms_chunk = _chunk_id(db, "perlindungan maklumat")
    mixed_chunk = _chunk_id(db, "Quarterly review")
    secret_chunk = _chunk_id(db, "Confidential bonus")

    texts = workspace["texts"]
    cases = [
        EvaluationCase(
            case_id=f"positive-{lang}", case_class="positive", language=lang,
            question=texts[lang], expected_passage_ids=[chunk_id],
        )
        for lang, chunk_id in (
            ("en", en_chunk), ("zh-Hans", zh_hans_chunk), ("zh-Hant", zh_hant_chunk),
            ("ms", ms_chunk), ("mixed", mixed_chunk),
        )
    ]
    cases.append(EvaluationCase(
        case_id="unauthorized-member", case_class="unauthorized", language="en",
        question=texts["en"], expected_passage_ids=[],
        forbidden_passage_ids=[secret_chunk], expected_answerable=False,
    ))
    results = [
        EvaluationResult(case_id=case.case_id, ranked_passage_ids=[
            item.chunk_id for item in _retrieve(
                db, workspace,
                _member(workspace) if case.case_id == "unauthorized-member"
                else _owner(workspace),
                case.question,
            ).items
        ])
        for case in cases
    ]

    report = evaluate(cases, results)
    # Retrieval binds the ranking and leakage gates per language; the
    # insufficient-evidence gates need the Task 14 answering layer.
    retrieval_failures = {
        name: [f for f in stratum.failures if not f.startswith("insufficient_")]
        for name, stratum in report.strata.items()
    }
    assert all(not failures for failures in retrieval_failures.values()), retrieval_failures
    # The member's ranking must not contain the restricted chunk at all.
    member_result = next(r for r in results if r.case_id == "unauthorized-member")
    assert secret_chunk not in member_result.ranked_passage_ids


def test_query_embedding_dimension_mismatch_is_typed(db, workspace) -> None:
    from ai_providers.contracts import EmbeddingDimensionError

    tiny_profile = ModelProfile(
        name="tiny", chat_model="fake-chat", embedding_model="fake-embed",
        embedding_dimensions=4,
    )

    async def run(engine, uow, _store) -> None:
        with pytest.raises(EmbeddingDimensionError):
            await retrieve(
                uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
                context=_owner(workspace), query=workspace["texts"]["en"],
                profile=tiny_profile,
            )

    _run(db, run)
