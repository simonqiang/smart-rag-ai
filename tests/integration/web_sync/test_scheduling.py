"""Task 21a: site sync scheduling, unchanged reuse, and cutover ordering.

Clock-controlled ``run_sync`` against the real database with a fake site;
extraction, indexing, and activation run the real pipeline (fake embeddings).
The acceptance ordering — removed pages leave retrieval only when the
complete indexed snapshot activates — is proven by querying retrieval between
every stage of the replacement chain.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
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
from ingestion.extraction import SourceObject, extract
from ingestion.parsers.snapshot import SNAPSHOT_MEDIA_TYPE
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from retrieval_answering.retrieval import retrieve
from source_catalog.catalog import CollectionNotFound
from source_catalog.version_service import archive_source
from web_sync.crawler import CrawlDefinition, CrawlPolicy, FetchResponse, crawl
from web_sync.scheduler import (
    CutoverPending,
    ScheduleNotFound,
    run_cutover,
    run_sync,
    schedule_sync,
)

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

START = "https://docs.example.com/docs/"
ZH_PAGE = "https://docs.example.com/docs/zh"
ZH_TEXT = "智能检索助手面向中小企业，提供本地知识问答服务，答案必须引用来源。"

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
INTERVAL = 3600


def _site(pages: dict[str, str]) -> dict[str, FetchResponse]:
    return {
        url: FetchResponse(200, "text/html", body.encode())
        for url, body in pages.items()
    }


def _pages(p1: str = "Share access keys only through the vault.") -> dict[str, str]:
    return {
        START: f"<html><body><h1>Acceptable use</h1><p>{p1}</p>"
               f'<a href="/docs/zh">Chinese guide</a></body></html>',
        ZH_PAGE: f"<html><body><p>{ZH_TEXT}</p></body></html>",
    }


def _crawler(site: dict[str, FetchResponse]):
    def resolve(host: str) -> list[str]:
        return ["93.184.216.34"] if host == "docs.example.com" else []

    def fetch(url: str) -> FetchResponse:
        return site.get(url, FetchResponse(404, "text/html", b"gone"))

    def crawl_site(definition: CrawlDefinition):
        return crawl(definition, resolve=resolve, fetch=fetch)

    return crawl_site


def _policy() -> CrawlPolicy:
    return CrawlPolicy(allowed_hosts=["docs.example.com"], allowed_prefixes=("/docs/",))


def _run(db: Settings, coro_factory, store_root: Path) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        try:
            return await coro_factory(
                engine, UnitOfWork(engine), ObjectStore(store_root),
            )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _schedule(db: Settings, workspace: dict, *, interval: int = INTERVAL,
              now: datetime = T0) -> dict:
    async def run(_engine, uow, _store) -> dict:
        schedule = await schedule_sync(
            uow, EventWriter(),
            context=workspace["owner_context"],
            collection_id=workspace["collection_id"],
            name="Docs handbook",
            start_url=START,
            interval_seconds=interval,
            now=now,
        )
        return {"schedule_id": schedule.schedule_id, "next_sync_at": schedule.next_sync_at}

    return _run(db, run, Path("/tmp/unused"))


def _sync(db: Settings, workspace: dict, schedule_id: str, site: dict, *, now: datetime,
          force: bool = False, store_root: Path | None = None) -> str:
    async def run(engine, _uow, store: ObjectStore) -> str:
        return await run_sync(
            engine, EventWriter(), store,
            schedule_id=schedule_id, now=now, force=force,
            crawl_site=_crawler(site),
        )

    return _run(db, run, store_root or _any_store())


def _any_store() -> Path:
    from tempfile import mkdtemp

    return Path(mkdtemp())


def _cutover(db: Settings, workspace: dict, schedule_id: str, version_id: str,
             now: datetime = T0) -> str:
    async def run(engine, _uow, _store) -> str:
        return await run_cutover(
            engine, EventWriter(), schedule_id=schedule_id, version_id=version_id, now=now,
        )

    return _run(db, run, _any_store())


def _index_version(db: Settings, store_root: Path, version_id: str) -> str:
    """The extract → index → activate_generation half of the worker chain."""

    async def real(engine, uow, store: ObjectStore) -> str:
        async with engine.begin() as connection:
            version = (
                await connection.execute(
                    text(
                        "SELECT workspace_id, object_sha256, media_type, filename, state "
                        "FROM source_versions WHERE id = :id"
                    ),
                    {"id": version_id},
                )
            ).mappings().one()
        document = extract(SourceObject(
            source_version_id=version_id,
            media_type=version["media_type"],
            filename=version["filename"],
            data=store.get(version["object_sha256"]),
        ))
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
                {"id": version_id},
            )
        staged = await index_version(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            workspace_id=str(version["workspace_id"]),
            source_version_id=version_id, document=document, profile=TEST_PROFILE,
        )
        await activate_generation(
            uow, EventWriter(), staged.generation_id,
            workspace_id=str(version["workspace_id"]),
        )
        async with engine.begin() as connection:
            return (
                await connection.execute(
                    text("SELECT state FROM source_versions WHERE id = :id"),
                    {"id": version_id},
                )
            ).scalar_one()

    return _run(db, real, store_root)


def _rows(db: Settings, query: str, params: dict) -> list[dict]:
    async def run(engine, _uow, _store) -> list[dict]:
        async with engine.begin() as connection:
            return [
                dict(row)
                for row in (
                    await connection.execute(text(query), params)
                ).mappings().all()
            ]

    return _run(db, run, _any_store())


def _active_chunk_hits(db: Settings, needle: str) -> int:
    """Chunks reachable by retrieval right now (active generation AND version)."""

    async def run(engine, _uow, _store) -> int:
        async with engine.begin() as connection:
            return (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM index_chunks c "
                        "JOIN index_generations g ON g.id = c.generation_id "
                        "AND g.state = 'active' "
                        "JOIN source_versions v ON v.id = c.source_version_id "
                        "AND v.state = 'active' "
                        "WHERE c.text LIKE :needle"
                    ),
                    {"needle": f"%{needle}%"},
                )
            ).scalar_one()

    return _run(db, run, _any_store())


def _row(db: Settings, query: str, params: dict) -> dict:
    rows = _rows(db, query, params)
    assert rows, f"expected one row for {query}"
    return rows[0]


def _retrieve(db: Settings, workspace: dict, query: str) -> list:
    async def run(_engine, uow, _store) -> list:
        hits = await retrieve(
            uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
            context=workspace["owner_context"], query=query, profile=TEST_PROFILE,
        )
        return list(hits.items)

    return _run(db, run, _any_store())


def test_schedule_registers_due_now_and_dispatches_first_window(
    db, workspace_with_members,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    assert schedule["next_sync_at"] == T0  # due immediately

    row = _row(db, "SELECT * FROM site_sync_schedules", {})
    assert row["name"] == "Docs handbook"
    assert row["source_id"] is None
    assert row["enabled"] is True

    jobs = _rows(
        db,
        "SELECT j.type, j.idempotency_key, j.status FROM jobs j "
        "WHERE j.type = 'site_sync'",
        {},
    )
    assert [(job["type"], job["status"]) for job in jobs] == [("site_sync", "pending")]
    assert jobs[0]["idempotency_key"] == f"site_sync:{schedule['schedule_id']}:{T0.isoformat()}"


def test_run_sync_before_due_is_not_due(db, workspace_with_members, store_root) -> None:
    schedule = _schedule(db, workspace_with_members)
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(_pages()),
        now=T0 - timedelta(seconds=1),
    )
    assert result == "not_due"
    assert _rows(db, "SELECT id FROM sources", {}) == []


def test_run_sync_force_runs_before_due(db, workspace_with_members, store_root) -> None:
    schedule = _schedule(db, workspace_with_members)
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(_pages()),
        now=T0 - timedelta(seconds=1), force=True,
    )
    assert result == "staged"
    assert _rows(db, "SELECT id FROM sources", {})


def test_disabled_schedule_skips(db, workspace_with_members, store_root) -> None:
    schedule = _schedule(db, workspace_with_members)

    async def run(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE site_sync_schedules SET enabled = false WHERE id = :id"),
                {"id": schedule["schedule_id"]},
            )

    _run(db, run, store_root)
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0,
    )
    assert result == "disabled"
    assert _rows(db, "SELECT id FROM sources", {}) == []


def test_first_sync_stages_then_cutover_syncs(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0, store_root=store_root
    )
    assert result == "staged"

    version = _row(db, "SELECT * FROM source_versions", {})
    assert version["state"] == "uploaded"
    assert version["media_type"] == SNAPSHOT_MEDIA_TYPE
    source = _row(db, "SELECT id, state FROM sources", {})
    assert source["state"] == "active"

    jobs = _rows(
        db, "SELECT type FROM jobs WHERE type IN ('source_extract', 'site_cutover') "
        "ORDER BY type", {},
    )
    assert [job["type"] for job in jobs] == ["site_cutover", "source_extract"]

    row = _row(db, "SELECT * FROM site_sync_schedules", {})
    assert row["source_id"] == source["id"]
    assert row["last_state"] == "staged"
    assert row["next_sync_at"] == T0 + timedelta(seconds=INTERVAL)

    # The staged window is claimed: a redelivered job in the same window is
    # not due even though next_sync_at is in the future anyway.
    again = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0, store_root=store_root
    )
    assert again == "not_due"

    state = _index_version(db, store_root, str(version["id"]))
    assert state == "active"  # first version auto-promotes

    result = _cutover(
        db, workspace_with_members, schedule["schedule_id"], str(version["id"]),
    )
    assert result == "synced"
    row = _row(db, "SELECT last_state, last_synced_at FROM site_sync_schedules", {})
    assert row["last_state"] == "synced"
    assert row["last_synced_at"] is not None

    hits = _retrieve(db, workspace_with_members, "access keys vault")
    assert hits, "synced snapshot must be retrievable"
    assert any(item.page == 2 for item in _retrieve(db, workspace_with_members, ZH_TEXT))


def test_removed_pages_leave_retrieval_only_at_cutover(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0,
         store_root=store_root)
    first = _row(db, "SELECT * FROM source_versions", {})
    _index_version(db, store_root, str(first["id"]))
    _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]))

    # Site changed: page 1 edited, Chinese page removed.
    later = T0 + timedelta(seconds=INTERVAL)
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"],
        _site({START: "<html><body><p>Rotated credentials live in the vault.</p></body></html>"}),
        now=later, store_root=store_root
    )
    assert result == "staged"
    versions = _rows(
        db, "SELECT id, state FROM source_versions ORDER BY created_at, id", {},
    )
    assert len(versions) == 2
    replacement = versions[-1]
    assert replacement["state"] == "uploaded"

    # Prior snapshot still serves retrieval, including the page that will die.
    assert _active_chunk_hits(db, ZH_TEXT) == 1

    state = _index_version(db, store_root, str(replacement["id"]))
    assert state == "indexed"  # replacement waits: an active sibling exists
    assert _active_chunk_hits(db, ZH_TEXT) == 1, (
        "removed page must still be retrievable before cutover"
    )

    result = _cutover(
        db, workspace_with_members, schedule["schedule_id"], str(replacement["id"]),
        now=later
    )
    assert result == "synced"

    states = _rows(
        db, "SELECT id, state FROM source_versions ORDER BY created_at, id", {},
    )
    assert [row["state"] for row in states] == ["superseded", "active"]
    assert _active_chunk_hits(db, ZH_TEXT) == 0, (
        "removed page must leave retrieval exactly at cutover"
    )
    assert _active_chunk_hits(db, "Rotated credentials") == 1
    hits = _retrieve(db, workspace_with_members, "rotated credentials")
    assert hits, "replacement content must be live after cutover"


def test_unchanged_recrawl_reuses_the_artifact(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0,
         store_root=store_root)
    first = _row(db, "SELECT * FROM source_versions", {})
    _index_version(db, store_root, str(first["id"]))
    _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]))

    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(_pages()),
        now=T0 + timedelta(seconds=INTERVAL), store_root=store_root
    )
    assert result == "unchanged"
    assert len(_rows(db, "SELECT id FROM source_versions", {})) == 1  # no new version
    assert len(_rows(db, "SELECT id FROM jobs WHERE type = 'site_cutover'", {})) == 1
    objects = list((store_root / "objects").iterdir())
    assert len(objects) == 1  # artifact reused
    row = _row(db, "SELECT last_state, last_synced_at FROM site_sync_schedules", {})
    assert row["last_state"] == "unchanged"
    assert row["last_synced_at"] is not None


def test_failed_crawl_retains_the_prior_snapshot(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0,
         store_root=store_root)
    first = _row(db, "SELECT * FROM source_versions", {})
    _index_version(db, store_root, str(first["id"]))
    _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]))

    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], {},  # every fetch 404s
        now=T0 + timedelta(seconds=INTERVAL),
    )
    assert result == "failed"
    assert len(_rows(db, "SELECT id FROM source_versions", {})) == 1
    row = _row(db, "SELECT last_state, last_detail FROM site_sync_schedules", {})
    assert row["last_state"] == "failed"
    assert row["last_detail"]
    assert _retrieve(db, workspace_with_members, "access keys vault"), (
        "failed crawl must not disturb the active snapshot"
    )


def test_partial_crawl_does_not_stage(db, workspace_with_members, store_root) -> None:
    schedule = _schedule(db, workspace_with_members)
    pages = _pages()
    del pages[ZH_PAGE]  # the link dangles: crawl succeeds partially, with warnings
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"], _site(pages), now=T0,
    )
    assert result == "failed"
    assert _rows(db, "SELECT id FROM sources", {}) == []
    row = _row(db, "SELECT last_state, last_detail FROM site_sync_schedules", {})
    assert row["last_state"] == "failed"
    assert "zh" in row["last_detail"]


def test_cutover_pending_until_indexed_then_syncs(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0,
         store_root=store_root)
    first = _row(db, "SELECT * FROM source_versions", {})
    _index_version(db, store_root, str(first["id"]))
    _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]))

    later = T0 + timedelta(seconds=INTERVAL)
    _sync(
        db, workspace_with_members, schedule["schedule_id"],
        _site({START: "<html><body><p>Rotated credentials live in the vault.</p></body></html>"}),
        now=later, store_root=store_root
    )
    replacement = _rows(db, "SELECT id FROM source_versions ORDER BY created_at, id", {})[-1]
    with pytest.raises(CutoverPending):
        _cutover(db, workspace_with_members, schedule["schedule_id"], replacement["id"], now=later)

    _index_version(db, store_root, replacement["id"])
    result = _cutover(
        db, workspace_with_members, schedule["schedule_id"], replacement["id"], now=later
    )
    assert result == "synced"


def test_cutover_of_failed_version_records_failure(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()), now=T0,
         store_root=store_root)
    first = _row(db, "SELECT * FROM source_versions", {})
    _index_version(db, store_root, str(first["id"]))
    _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]))

    later = T0 + timedelta(seconds=INTERVAL)
    _sync(
        db, workspace_with_members, schedule["schedule_id"],
        _site({START: "<html><body><p>Rotated credentials live in the vault.</p></body></html>"}),
        now=later, store_root=store_root
    )
    replacement = _rows(db, "SELECT id FROM source_versions ORDER BY created_at, id", {})[-1]

    async def fail(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE source_versions SET state = 'failed' WHERE id = :id"),
                {"id": replacement["id"]},
            )

    _run(db, fail, store_root)
    result = _cutover(
        db, workspace_with_members, schedule["schedule_id"], replacement["id"], now=later
    )
    assert result == "failed"
    row = _row(db, "SELECT last_state FROM site_sync_schedules", {})
    assert row["last_state"] == "failed"
    states = [r["state"] for r in _rows(db, "SELECT state FROM source_versions", {})]
    assert states.count("active") == 1  # prior snapshot untouched


def test_member_cannot_schedule_and_unknown_collection_is_generic(
    db, workspace_with_members, store_root,
) -> None:
    async def run(_engine, uow, _store) -> tuple:
        member_denied = collection_missing = None
        try:
            await schedule_sync(
                uow, EventWriter(),
                context=workspace_with_members["granted"]["context"],
                collection_id=workspace_with_members["collection_id"],
                name="x", start_url=START, now=T0,
            )
        except AccessDenied as failure:
            member_denied = failure
        try:
            await schedule_sync(
                uow, EventWriter(),
                context=workspace_with_members["owner_context"],
                collection_id=str(uuid.uuid4()),
                name="x", start_url=START, now=T0,
            )
        except CollectionNotFound as failure:
            collection_missing = failure
        return member_denied, collection_missing

    member_denied, collection_missing = _run(db, run, store_root)
    assert member_denied is not None
    assert collection_missing is not None


def test_schedule_rejects_too_short_interval(db, workspace_with_members, store_root) -> None:
    async def run(_engine, uow, _store) -> None:
        with pytest.raises(ValueError):
            await schedule_sync(
                uow, EventWriter(),
                context=workspace_with_members["owner_context"],
                collection_id=workspace_with_members["collection_id"],
                name="x", start_url=START, interval_seconds=60, now=T0,
            )

    _run(db, run, store_root)


def test_foreign_collection_is_generic(db, workspace_with_members, store_root) -> None:
    foreign_workspace = str(uuid.uuid4())
    foreign_collection = str(uuid.uuid4())

    async def run(_engine, uow, _store) -> None:
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_workspace},
            )
            await transaction.execute(
                text("INSERT INTO collections (id, workspace_id, name) "
                     "VALUES (:id, :workspace_id, 'Secret')"),
                {"id": foreign_collection, "workspace_id": foreign_workspace},
            )
        with pytest.raises(CollectionNotFound):
            await schedule_sync(
                uow, EventWriter(),
                context=workspace_with_members["owner_context"],
                collection_id=foreign_collection,
                name="x", start_url=START, now=T0,
            )

    _run(db, run, store_root)


def test_unknown_schedule_and_version_are_not_found(
    db, workspace_with_members, store_root,
) -> None:
    missing = str(uuid.uuid4())
    with pytest.raises(ScheduleNotFound):
        _sync(db, workspace_with_members, missing, _site(_pages()), now=T0)
    with pytest.raises(ScheduleNotFound):
        _cutover(db, workspace_with_members, missing, missing, now=T0)
    schedule = _schedule(db, workspace_with_members)
    with pytest.raises(ScheduleNotFound):
        _cutover(db, workspace_with_members, schedule["schedule_id"], missing, now=T0)


def test_archived_source_blocks_staging(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()),
          now=T0, store_root=store_root)
    source = _row(db, "SELECT id FROM sources", {})

    async def archive(_engine, uow, _store) -> None:
        await archive_source(
            uow, EventWriter(),
            context=workspace_with_members["owner_context"], source_id=str(source["id"]),
        )

    _run(db, archive, store_root)
    result = _sync(
        db, workspace_with_members, schedule["schedule_id"],
        _site({START: "<html><body><p>Rotated credentials live in the vault.</p></body></html>"}),
        now=T0 + timedelta(seconds=INTERVAL), store_root=store_root,
    )
    assert result == "blocked"
    assert len(_rows(db, "SELECT id FROM source_versions", {})) == 1


def test_cutover_refuses_superseded_version(
    db, workspace_with_members, store_root,
) -> None:
    schedule = _schedule(db, workspace_with_members)
    _sync(db, workspace_with_members, schedule["schedule_id"], _site(_pages()),
          now=T0, store_root=store_root)
    first = _row(db, "SELECT * FROM source_versions", {})
    _index_version(db, store_root, str(first["id"]))
    _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]))

    later = T0 + timedelta(seconds=INTERVAL)
    _sync(
        db, workspace_with_members, schedule["schedule_id"],
        _site({START: "<html><body><p>Rotated credentials live in the vault.</p></body></html>"}),
        now=later, store_root=store_root,
    )
    replacement = _rows(db, "SELECT id FROM source_versions ORDER BY created_at, id", {})[-1]
    _index_version(db, store_root, replacement["id"])
    _cutover(db, workspace_with_members, schedule["schedule_id"], replacement["id"], now=later)

    with pytest.raises(CutoverPending):
        _cutover(db, workspace_with_members, schedule["schedule_id"], str(first["id"]), now=later)
