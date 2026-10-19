"""Task 20c: crawl → stage → extract → index → activate, and sha reuse.

The crawl runs against an in-memory fake site (same seam discipline as the
Task 20b security proofs); staging, extraction, indexing, activation, and
retrieval run against the real pipeline with fake embeddings.
"""

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessDenied
from ingestion.extraction import ExtractionFailed, SourceObject, extract
from ingestion.parsers.snapshot import SNAPSHOT_MEDIA_TYPE
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from retrieval_answering.retrieval import retrieve
from source_catalog.catalog import CollectionNotFound
from web_sync.crawler import CrawlDefinition, CrawlPolicy, FetchResponse, crawl
from web_sync.snapshots import build_snapshot, stage_snapshot

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

START = "https://docs.example.com/docs/"
ZH_PAGE = "https://docs.example.com/docs/zh"
ZH_TEXT = "智能检索助手面向中小企业，提供本地知识问答服务，答案必须引用来源。"

SITE = {
    START: FetchResponse(
        200, "text/html",
        b"<html><head><title>Handbook</title></head><body>"
        b"<h1>Acceptable use</h1><p>Share access keys only through the vault.</p>"
        b'<a href="/docs/zh">Chinese guide</a></body></html>',
    ),
    ZH_PAGE: FetchResponse(
        200, "text/html",
        f"<html><body><p>{ZH_TEXT}</p></body></html>".encode(),
    ),
}

POLICY = CrawlPolicy(allowed_hosts=["docs.example.com"], allowed_prefixes=("/docs/",))


def _fake_site():
    def resolve(host: str) -> list[str]:
        return ["93.184.216.34"] if host == "docs.example.com" else []

    def fetch(url: str) -> FetchResponse:
        return SITE[url]

    return resolve, fetch


def _crawl() -> object:
    resolve, fetch = _fake_site()
    return crawl(CrawlDefinition(START, POLICY), resolve=resolve, fetch=fetch)


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


def _stage(db: Settings, store_root: Path, workspace: dict, bundle) -> object:
    async def run(_engine, uow, store: ObjectStore) -> object:
        return await stage_snapshot(
            uow, EventWriter(), store,
            context=workspace["owner_context"],
            collection_id=workspace["collection_id"],
            name="Docs handbook",
            start_url=START,
            bundle=bundle,
        )

    return _run(db, run, store_root)


def _index_and_activate(db: Settings, store_root: Path, staged: dict) -> object:
    """The extract → index → activate half of the worker chain (fake embeddings)."""

    async def run(engine, uow, store: ObjectStore) -> object:
        document = extract(SourceObject(
            source_version_id=staged["source_version_id"],
            media_type=staged["media_type"],
            filename=staged["filename"],
            data=store.get(staged["checksum"]),
        ))
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
                {"id": staged["source_version_id"]},
            )
        staged_generation = await index_version(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            workspace_id=staged["workspace_id"],
            source_version_id=staged["source_version_id"],
            document=document,
            profile=TEST_PROFILE,
        )
        return document, await activate_generation(
            uow, EventWriter(), staged_generation.generation_id,
            workspace_id=staged["workspace_id"],
        )

    return _run(db, run, store_root)


def test_crawl_stages_through_the_full_pipeline(
    db, workspace_with_members, store_root,
) -> None:
    result = _crawl()
    bundle = build_snapshot(result)
    staged = _stage(db, store_root, workspace_with_members, bundle)
    assert staged.page_count == 2
    assert staged.checksum == hashlib.sha256(bundle.data).hexdigest()
    staged = {
        "source_id": staged.source_id,
        "source_version_id": staged.source_version_id,
        "checksum": staged.checksum,
        "media_type": SNAPSHOT_MEDIA_TYPE,
        "filename": "docs.example.com.snapshot.json",
        "workspace_id": workspace_with_members["workspace_id"],
    }

    async def verify_rows() -> tuple:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                version = (
                    await connection.execute(
                        text(
                            "SELECT state, media_type, filename, size_bytes, object_sha256 "
                            "FROM source_versions WHERE id = :id"
                        ),
                        {"id": staged["source_version_id"]},
                    )
                ).mappings().one()
                outbox_rows = (
                    await connection.execute(
                        text(
                            "SELECT o.topic, j.status, j.idempotency_key "
                            "FROM outbox o JOIN jobs j "
                            "ON j.id = CAST(o.payload->>'job_id' AS uuid)"
                        )
                    )
                ).mappings().all()
        finally:
            await engine.dispose()
        return dict(version), [dict(row) for row in outbox_rows]

    version, outbox_rows = _run(db, lambda *a: verify_rows())
    assert version["state"] == "uploaded"
    assert version["media_type"] == SNAPSHOT_MEDIA_TYPE
    assert version["filename"] == "docs.example.com.snapshot.json"
    assert version["size_bytes"] == len(bundle.data)
    assert version["object_sha256"] == staged["checksum"]
    # Dispatch is a committed outbox row with the standard idempotency key.
    assert [row["topic"] for row in outbox_rows] == ["source_extract"]
    assert outbox_rows[0]["idempotency_key"] == f"source_extract:{staged['source_version_id']}"
    assert outbox_rows[0]["status"] == "pending"

    document, generation = _index_and_activate(db, store_root, staged)
    assert generation.state == "active"
    assert [block.text for block in document.blocks] == [
        "Handbook",  # <title>
        "Acceptable use",
        "Share access keys only through the vault.",
        "Chinese guide",
        ZH_TEXT,
    ]
    assert [block.location.page for block in document.blocks] == [1, 1, 1, 1, 2]

    async def chunk_pages() -> list[int]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return [
                    int(page)
                    for page in (
                        await connection.execute(
                            text(
                                "SELECT DISTINCT page FROM index_chunks "
                                "WHERE source_version_id = :id ORDER BY page"
                            ),
                            {"id": staged["source_version_id"]},
                        )
                    ).scalars()
                ]
        finally:
            await engine.dispose()

    assert _run(db, lambda *a: chunk_pages()) == [1, 2]  # pages survive into index_chunks


async def _retrieve(uow, context, query: str) -> object:
    return await retrieve(
        uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
        context=context, query=query, profile=TEST_PROFILE,
    )


def test_retrieval_honors_grant_filters_for_snapshot_content(
    db, workspace_with_members, store_root,
) -> None:
    staged = _stage(db, store_root, workspace_with_members, build_snapshot(_crawl()))
    _index_and_activate(db, store_root, {
        "source_id": staged.source_id,
        "source_version_id": staged.source_version_id,
        "checksum": staged.checksum,
        "media_type": SNAPSHOT_MEDIA_TYPE,
        "filename": staged.filename,
        "workspace_id": workspace_with_members["workspace_id"],
    })

    async def run(_engine, uow, _store) -> tuple:
        owner_hits = await _retrieve(
            uow, workspace_with_members["owner_context"], "access keys vault"
        )
        granted_hits = await _retrieve(
            uow, workspace_with_members["granted"]["context"], ZH_TEXT
        )
        ungranted = await _retrieve(
            uow, workspace_with_members["ungranted"]["context"], "access keys vault"
        )
        return owner_hits, granted_hits, ungranted

    owner_hits, granted_hits, ungranted = _run(db, run)

    assert owner_hits.items, "owner must see the snapshot content"
    assert all(item.source_id == staged.source_id for item in owner_hits.items)
    assert owner_hits.items[0].page == 1
    assert any(item.page == 2 for item in granted_hits.items)
    assert ungranted.items == []  # grants fail closed


def test_unchanged_recrawl_reuses_the_stored_object(
    db, workspace_with_members, store_root,
) -> None:
    bundle = build_snapshot(_crawl())
    first = _stage(db, store_root, workspace_with_members, bundle)
    second = _stage(db, store_root, workspace_with_members, build_snapshot(_crawl()))

    # New version row, same content-addressed object: only one file on disk.
    assert second.checksum == first.checksum
    assert second.source_version_id != first.source_version_id
    objects = list((store_root / "objects").iterdir())
    assert [path.name for path in objects] == [first.checksum]


def test_member_cannot_stage_and_foreign_collection_is_generic(
    db, workspace_with_members, store_root,
) -> None:
    bundle = build_snapshot(_crawl())

    async def run(_engine, uow, store: ObjectStore) -> tuple:
        member_denied = None
        collection_error = None
        try:
            await stage_snapshot(
                uow, EventWriter(), store,
                context=workspace_with_members["granted"]["context"],
                collection_id=workspace_with_members["collection_id"],
                name="x", start_url=START, bundle=bundle,
            )
        except AccessDenied as failure:
            member_denied = failure
        foreign = dict(workspace_with_members)
        foreign["collection_id"] = "00000000-0000-0000-0000-000000000000"
        try:
            await stage_snapshot(
                uow, EventWriter(), store,
                context=workspace_with_members["owner_context"],
                collection_id=foreign["collection_id"],
                name="x", start_url=START, bundle=bundle,
            )
        except CollectionNotFound as failure:
            collection_error = failure
        return member_denied, collection_error

    member_denied, collection_error = _run(db, run, store_root)
    assert member_denied is not None
    assert collection_error is not None


def test_worker_extract_refuses_corrupt_bundle_via_extractor_routing(
    db, workspace_with_members, store_root,
) -> None:
    staged = _stage(db, store_root, workspace_with_members, build_snapshot(_crawl()))
    with pytest.raises(ExtractionFailed) as caught:
        extract(SourceObject(
            source_version_id=staged.source_version_id,
            media_type=SNAPSHOT_MEDIA_TYPE,
            filename=staged.filename,
            data=b'{"version": 1, "pages": "corrupt"}',
        ))
    assert caught.value.reason == "corrupt_snapshot"
