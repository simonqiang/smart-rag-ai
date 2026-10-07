"""Task 12: chunk, embed, and activate the first index generation."""

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.contracts import EmbeddingDimensionError, ProviderUnavailableError
from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from ingestion.extraction import SourceObject, extract
from knowledge_index.generations import ActivationError, activate_generation
from knowledge_index.indexer import IndexingError, index_version

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)


def _run(db: Settings, coro_factory, store_root: Path | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        store = ObjectStore(store_root) if store_root else None
        try:
            return await coro_factory(engine, uow, store)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _index(db: Settings, workspace: dict, indexed: dict, store_root: Path,
           embeddings: FakeEmbeddingProvider | None = None) -> object:
    async def run(_engine, uow, _store) -> object:
        return await index_version(
            uow, embeddings or FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            workspace_id=workspace["workspace_id"],
            source_version_id=indexed["version_id"],
            document=indexed["document"],
            profile=TEST_PROFILE,
        )

    return _run(db, run, store_root)


def _activate(db: Settings, workspace: dict, generation_id: str) -> object:
    async def run(_engine, uow, _store) -> object:
        return await activate_generation(
            uow, EventWriter(), generation_id, workspace_id=workspace["workspace_id"],
        )

    return _run(db, run)


def test_index_stages_chunks_embeddings_and_keyword_vectors(
    db, workspace, indexed, store_root,
) -> None:
    result = _index(db, workspace, indexed, store_root)

    assert result.duplicate is False
    assert result.state == "staging"
    assert result.chunk_count == 1  # both short blocks pack into one chunk
    assert result.compatibility_key == "fake-embed:8:chunk-v1:lang-v1"

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            generation = (
                await transaction.execute(
                    text("SELECT state, dimension, model, chunk_count FROM index_generations")
                )
            ).mappings().one()
            chunks = (
                await transaction.execute(
                    text("SELECT ordinal, language, page, block_start, block_end, "
                         "vector_dims(embedding) AS dims FROM index_chunks ORDER BY ordinal")
                )
            ).mappings().all()
            keyword_hits = (
                await transaction.execute(
                    text(
                        "SELECT count(*) FROM index_chunks WHERE keywords @@ "
                        "plainto_tsquery('english', 'grounded evidence')"
                    )
                )
            ).scalar_one()
            version_state = (
                await transaction.execute(
                    text("SELECT state FROM source_versions")
                )
            ).scalar_one()
        return dict(generation), [dict(c) for c in chunks], keyword_hits, version_state

    generation, chunks, keyword_hits, version_state = _run(db, verify, store_root)
    assert generation == {
        "state": "staging", "dimension": 8, "model": "fake-embed", "chunk_count": 1,
    }
    assert [c["dims"] for c in chunks] == [8]
    assert chunks[0]["language"] == "en"
    # text/plain has no pages: the block ordinal is the citation anchor.
    assert (chunks[0]["page"], chunks[0]["block_start"], chunks[0]["block_end"]) == (None, 0, 0)
    assert keyword_hits >= 1  # language-aware keyword vector matched at index time
    assert version_state == "extracted"  # activation is a separate, explicit step


def test_activate_generation_flips_pointers_atomically(
    db, workspace, indexed, store_root,
) -> None:
    staged = _index(db, workspace, indexed, store_root)
    activated = _activate(db, workspace, staged.generation_id)

    assert activated.state == "active"

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            states = (
                await transaction.execute(
                    text("SELECT state FROM index_generations")
                )
            ).scalars().all()
            version_state = (
                await transaction.execute(
                    text("SELECT state FROM source_versions")
                )
            ).scalar_one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type LIKE 'source.index%'")
                )
            ).scalars().all()
        return list(states), version_state, list(audits)

    states, version_state, audits = _run(db, verify, store_root)
    assert states == ["active"]
    assert version_state == "indexed"
    assert audits == ["source.index_activated"]


def test_activation_replaces_the_previous_generation(
    db, workspace, indexed, store_root,
) -> None:
    first = _index(db, workspace, indexed, store_root)
    _activate(db, workspace, first.generation_id)

    # Simulate a replacement build: version back to extracted, restage, activate.
    async def restage(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
                {"id": indexed["version_id"]},
            )

    _run(db, restage, store_root)
    second = _index(db, workspace, indexed, store_root)
    assert second.generation_id != first.generation_id
    _activate(db, workspace, second.generation_id)

    async def verify(_engine, uow, _store) -> list:
        async with uow.transaction() as transaction:
            return list(
                (
                    await transaction.execute(
                        text("SELECT state FROM index_generations ORDER BY state")
                    )
                ).scalars()
            )

    assert _run(db, verify, store_root) == ["active", "retired"]


def test_empty_content_refuses_to_index(db, workspace, store_root) -> None:
    async def run(engine, uow, store) -> None:
        from identity_access.authorization import AccessContext
        from ingestion.uploads import register_upload
        from source_catalog.catalog import create_collection  # noqa: F401

        context = AccessContext(
            user_id=workspace["owner_id"],
            workspace_id=workspace["workspace_id"],
            role="owner",
        )
        accepted = await register_upload(
            uow, EventWriter(), store,
            context=context,
            collection_id=workspace["collection_id"],
            name="Empty",
            filename="empty.txt",
            data=b"   ",
        )
        document = extract(SourceObject(
            source_version_id=accepted.source_version_id,
            media_type=accepted.media_type,
            filename="empty.txt",
            data=store.get(accepted.checksum),
        ))
        assert document.warnings == ["empty"]
        with pytest.raises(IndexingError) as excinfo:
            await index_version(
                uow, FakeEmbeddingProvider(8),
                workspace_id=workspace["workspace_id"],
                source_version_id=accepted.source_version_id,
                document=document,
                profile=TEST_PROFILE,
            )
        assert excinfo.value.reason == "empty_content"

    _run(db, run, store_root)

    async def verify(_engine, uow, _store) -> int:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(text("SELECT count(*) FROM index_generations"))
            ).scalar_one()

    assert _run(db, verify, store_root) == 0


def test_embedding_batch_failure_leaves_nothing_behind(
    db, workspace, indexed, store_root,
) -> None:
    embeddings = FakeEmbeddingProvider(
        TEST_PROFILE.embedding_dimensions,
        script=[ProviderUnavailableError("embed endpoint down")],
    )

    with pytest.raises(ProviderUnavailableError):
        _index(db, workspace, indexed, store_root, embeddings)

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            generations = (
                await transaction.execute(text("SELECT count(*) FROM index_generations"))
            ).scalar_one()
            chunks = (
                await transaction.execute(text("SELECT count(*) FROM index_chunks"))
            ).scalar_one()
            version_state = (
                await transaction.execute(text("SELECT state FROM source_versions"))
            ).scalar_one()
        return generations, chunks, version_state

    assert _run(db, verify, store_root) == (0, 0, "extracted")


def test_dimension_mismatch_refuses_before_persistence(
    db, workspace, indexed, store_root,
) -> None:
    # Fake provider builds 8-dim vectors; this profile expects 4.
    profile = ModelProfile(
        name="tiny", chat_model="fake-chat", embedding_model="fake-embed",
        embedding_dimensions=4,
    )

    async def run(_engine, uow, _store) -> None:
        with pytest.raises(EmbeddingDimensionError):
            await index_version(
                uow, FakeEmbeddingProvider(8),
                workspace_id=workspace["workspace_id"],
                source_version_id=indexed["version_id"],
                document=indexed["document"],
                profile=profile,
            )

    _run(db, run, store_root)

    async def verify(_engine, uow, _store) -> int:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(text("SELECT count(*) FROM index_chunks"))
            ).scalar_one()

    assert _run(db, verify, store_root) == 0


def test_duplicate_checksum_reindex_is_idempotent(
    db, workspace, indexed, store_root,
) -> None:
    embeddings = FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions)
    first = _index(db, workspace, indexed, store_root, embeddings)
    second = _index(db, workspace, indexed, store_root, embeddings)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.generation_id == first.generation_id
    assert len(embeddings.requests) == 1  # no second embedding pass

    _activate(db, workspace, first.generation_id)
    third = _index(db, workspace, indexed, store_root, embeddings)
    assert third.duplicate is True
    assert third.state == "active"
    assert len(embeddings.requests) == 1


def test_activation_rejections(db, workspace, indexed, store_root) -> None:
    with pytest.raises(ActivationError) as excinfo:
        _activate(db, workspace, str(uuid.uuid4()))
    assert excinfo.value.reason == "not_found"

    staged = _index(db, workspace, indexed, store_root)
    _activate(db, workspace, staged.generation_id)
    with pytest.raises(ActivationError) as excinfo:
        _activate(db, workspace, staged.generation_id)
    assert excinfo.value.reason == "not_staging"

    # A staging generation with no chunks refuses rather than activating air.
    empty_id = str(uuid.uuid4())

    async def seed_empty(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO index_generations (id, workspace_id, source_version_id, "
                    "compatibility_key, dimension, model, state, chunk_count) "
                    "VALUES (:id, :ws, :version, 'fake-embed:8:chunk-v1:lang-v1', 8, "
                    "'fake-embed', 'staging', 0)"
                ),
                {"id": empty_id, "ws": workspace["workspace_id"],
                 "version": indexed["version_id"]},
            )

    _run(db, seed_empty, store_root)
    with pytest.raises(ActivationError) as excinfo:
        _activate(db, workspace, empty_id)
    assert excinfo.value.reason == "empty"

    # A staged vector with the wrong dimension refuses activation.
    wrong_id = str(uuid.uuid4())

    async def seed_wrong_dims(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO index_generations (id, workspace_id, source_version_id, "
                    "compatibility_key, dimension, model, state, chunk_count) "
                    "VALUES (:id, :ws, :version, 'fake-embed:4:chunk-v1:lang-v1', 4, "
                    "'fake-embed', 'staging', 1)"
                ),
                {"id": wrong_id, "ws": workspace["workspace_id"],
                 "version": indexed["version_id"]},
            )
            await connection.execute(
                text(
                    "INSERT INTO index_chunks (id, generation_id, workspace_id, source_id, "
                    "source_version_id, ordinal, text, language, page, block_start, "
                    "block_end, embedding, keywords) "
                    "VALUES (:id, :generation, :ws, "
                    "(SELECT source_id FROM source_versions WHERE id = :version), "
                    ":version, 0, 'text', 'en', 1, 0, 0, "
                    "CAST('[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]' AS vector), "
                    "to_tsvector('simple', 'text'))"
                ),
                {"id": str(uuid.uuid4()), "generation": wrong_id,
                 "ws": workspace["workspace_id"], "version": indexed["version_id"]},
            )

    _run(db, seed_wrong_dims, store_root)
    with pytest.raises(ActivationError) as excinfo:
        _activate(db, workspace, wrong_id)
    assert excinfo.value.reason == "dimension_mismatch"

    async def verify(_engine, uow, _store) -> list:
        async with uow.transaction() as transaction:
            return list(
                (
                    await transaction.execute(
                        text("SELECT DISTINCT state FROM index_generations ORDER BY state")
                    )
                ).scalars()
            )

    # None of the rejected activations changed any state: the activated
    # generation stays active and the refused candidates stay staging.
    assert _run(db, verify, store_root) == ["active", "staging"]


def test_worker_indexes_and_activates(db, workspace, indexed, store_root) -> None:
    job = _enqueue(db, indexed["version_id"])

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    assert _run(db, run, store_root) == "indexed"

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            version_state = (
                await transaction.execute(text("SELECT state FROM source_versions"))
            ).scalar_one()
            generation_state = (
                await transaction.execute(text("SELECT state FROM index_generations"))
            ).scalar_one()
            job_status = (
                await transaction.execute(text("SELECT status FROM jobs"))
            ).scalar_one()
        return version_state, generation_state, job_status

    assert _run(db, verify, store_root) == ("indexed", "active", "completed")


def test_worker_completed_delivery_is_idempotent(
    db, workspace, indexed, store_root,
) -> None:
    job = _enqueue(db, indexed["version_id"])

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    assert _run(db, run, store_root) == "indexed"

    # A duplicate delivery of the completed job is claimed by nobody.
    assert _run(db, run, store_root) == "skipped"

    # A redelivery that re-claims (lease expired, job re-pended) sees the
    # already-indexed version and converges without re-embedding.
    async def reset_job(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET status = 'pending', lease_owner = NULL, "
                    "lease_expires_at = now() - interval '1 second'"
                )
            )

    _run(db, reset_job, store_root)
    assert _run(db, run, store_root) == "already_indexed"


def test_worker_unknown_version_fails_job(db, workspace, store_root) -> None:
    job = _enqueue(db, str(uuid.uuid4()))

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": str(uuid.uuid4())},
        )

    assert _run(db, run, store_root) == "missing_version"


def test_worker_embedding_outage_propagates_and_retry_recovers(
    db, workspace, indexed, store_root,
) -> None:
    job = _enqueue(db, indexed["version_id"])
    outage = FakeEmbeddingProvider(
        TEST_PROFILE.embedding_dimensions,
        script=[ProviderUnavailableError("embed endpoint down")],
    )

    async def run(engine, _uow, store, embeddings) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, embeddings,
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    with pytest.raises(ProviderUnavailableError):
        _run(db, lambda engine, uow, store: run(engine, uow, store, outage), store_root)

    # Version stays indexable and the lease expires, so redelivery recovers.
    async def expire_lease(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'")
            )

    _run(db, expire_lease, store_root)
    assert _run(
        db, lambda engine, uow, store: run(
            engine, uow, store, FakeEmbeddingProvider(8)
        ), store_root,
    ) == "indexed"


def _enqueue(db: Settings, version_id: str) -> uuid.UUID:
    async def run() -> uuid.UUID:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.transaction() as transaction:
                return await JobQueue().enqueue(
                    JobCommand(type="source_index", payload={"version_id": version_id}),
                    transaction,
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_actor_composes_settings_store_and_pipeline(
    db, workspace, indexed, store_root, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.worker.tasks import index as task_module

    job = _enqueue(db, indexed["version_id"])

    class FakeSettings:
        database_url = db.database_url
        data_dir = str(store_root)
        ollama_host = "http://127.0.0.1:11434"

        @classmethod
        def load(cls) -> "FakeSettings":
            return cls()

        @property
        def active_profile(self) -> ModelProfile:
            return TEST_PROFILE

    monkeypatch.setattr(task_module, "Settings", FakeSettings)
    monkeypatch.setattr(task_module, "ObjectStore", lambda _root: ObjectStore(store_root))
    monkeypatch.setattr(
        task_module, "OllamaEmbeddingProvider",
        lambda *_args: FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
    )

    assert task_module.source_index(
        job_id=str(job), version_id=indexed["version_id"]
    ) == "indexed"

    async def verify() -> tuple:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                version_state = (
                    await connection.execute(text("SELECT state FROM source_versions"))
                ).scalar_one()
                job_status = (
                    await connection.execute(text("SELECT status FROM jobs"))
                ).scalar_one()
        finally:
            await engine.dispose()
        return version_state, job_status

    assert asyncio.run(verify()) == ("indexed", "completed")


def test_worker_non_extracted_version_fails_job(
    db, workspace, indexed, store_root,
) -> None:
    job = _enqueue(db, indexed["version_id"])

    async def unextract(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE source_versions SET state = 'uploaded' WHERE id = :id"),
                {"id": indexed["version_id"]},
            )

    _run(db, unextract, store_root)

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    assert _run(db, run, store_root) == "not_indexable:uploaded"


def test_worker_empty_content_fails_version_and_audits(
    db, workspace, indexed, store_root,
) -> None:
    job = _enqueue(db, indexed["version_id"])

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    # Patch extract inside the task module, then deliver.
    from apps.worker.tasks import index as task_module
    from ingestion.extraction import ExtractedDocument

    original_extract = task_module.extract

    def blank(_source: object) -> ExtractedDocument:
        return ExtractedDocument(
            source_version_id=indexed["version_id"],
            media_type="text/plain", language="en", blocks=[], warnings=["empty"],
        )

    task_module.extract = blank
    try:
        assert _run(db, run, store_root) == "failed:empty_content"
    finally:
        task_module.extract = original_extract

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            version_state = (
                await transaction.execute(text("SELECT state FROM source_versions"))
            ).scalar_one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type = 'source.indexing_failed'")
                )
            ).scalars().all()
            generation_count = (
                await transaction.execute(text("SELECT count(*) FROM index_generations"))
            ).scalar_one()
        return version_state, list(audits), generation_count

    assert _run(db, verify, store_root) == ("failed", ["source.indexing_failed"], 0)


def test_worker_dimension_mismatch_fails_version(
    db, workspace, indexed, store_root,
) -> None:
    job = _enqueue(db, indexed["version_id"])
    profile = ModelProfile(
        name="tiny", chat_model="fake-chat", embedding_model="fake-embed",
        embedding_dimensions=4,
    )

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, profile, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    assert _run(db, run, store_root) == "failed:dimension_mismatch"

    async def verify(_engine, uow, _store) -> str:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(text("SELECT state FROM source_versions"))
            ).scalar_one()

    assert _run(db, verify, store_root) == "failed"


def test_worker_aborts_when_job_cancelled_after_claim(
    db, workspace, indexed, store_root, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from apps.worker.tasks import index as task_module
    from ingestion.extraction import ExtractedDocument

    job = _enqueue(db, indexed["version_id"])

    def cancel_during_extract(_source: object) -> ExtractedDocument:
        def cancel() -> None:
            async def run() -> None:
                engine = create_async_engine(db.database_url)
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE jobs SET status = 'cancelled' WHERE id = :id"),
                        {"id": str(job)},
                    )
                await engine.dispose()

            asyncio.run(run())

        thread = threading.Thread(target=cancel)
        thread.start()
        thread.join()
        return indexed["document"]

    monkeypatch.setattr(task_module, "extract", cancel_during_extract)

    async def run(engine, _uow, store) -> str:
        from apps.worker.tasks.index import index_payload

        return await index_payload(
            engine, store, TEST_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": str(job), "version_id": indexed["version_id"]},
        )

    assert _run(db, run, store_root) == "cancelled"

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            version_state = (
                await transaction.execute(text("SELECT state FROM source_versions"))
            ).scalar_one()
            states = (
                await transaction.execute(
                    text("SELECT state FROM index_generations")
                )
            ).scalars().all()
        return version_state, list(states)

    # Discardable work: the version stays extractable and nothing activates.
    assert _run(db, verify, store_root) == ("extracted", ["staging"])


def test_service_refuses_unknown_and_unindexable_versions(
    db, workspace, indexed, store_root,
) -> None:
    async def run(_engine, uow, _store) -> list:
        outcomes = []
        with pytest.raises(IndexingError) as excinfo:
            await index_version(
                uow, FakeEmbeddingProvider(8),
                workspace_id=workspace["workspace_id"],
                source_version_id=str(uuid.uuid4()),
                document=indexed["document"],
                profile=TEST_PROFILE,
            )
        outcomes.append(excinfo.value.reason)

        async def mark_uploaded(engine) -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE source_versions SET state = 'uploaded' WHERE id = :id"),
                    {"id": indexed["version_id"]},
                )

        engine = create_async_engine(db.database_url)
        try:
            await mark_uploaded(engine)
        finally:
            await engine.dispose()
        with pytest.raises(IndexingError) as excinfo:
            await index_version(
                uow, FakeEmbeddingProvider(8),
                workspace_id=workspace["workspace_id"],
                source_version_id=indexed["version_id"],
                document=indexed["document"],
                profile=TEST_PROFILE,
            )
        outcomes.append(excinfo.value.reason)
        return outcomes

    assert _run(db, run, store_root) == ["version_not_found", "not_extracted"]
