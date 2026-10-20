"""Task 21b: site sync worker actors and the relay due-scan.

Live Compose PostgreSQL; the crawl seam is a fake site and index readiness
is a direct state flip — the full crawl-to-activation chain is proven in
``test_scheduling.py``, these tests pin the delivery/lease semantics.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from web_sync.crawler import (
    CrawlDefinition,
    CrawlFailed,
    CrawlPolicy,
    FetchResponse,
    crawl,
)
from web_sync.scheduler import CutoverPending, schedule_sync

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = "https://docs.example.com/docs/"
BODY = b"<html><body><p>Share access keys only through the vault.</p></body></html>"


def _owner_context(workspace: dict) -> AccessContext:
    return AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"],
        role="owner", collection_ids=[workspace["collection_id"]],
    )


def _crawler(site: dict[str, FetchResponse]):
    def resolve(host: str) -> list[str]:
        return ["93.184.216.34"] if host == "docs.example.com" else []

    def fetch(url: str) -> FetchResponse:
        return site.get(url, FetchResponse(404, "text/html", b"gone"))

    def crawl_site(definition: CrawlDefinition):
        return crawl(definition, resolve=resolve, fetch=fetch)

    return crawl_site


def _crawler_site() -> dict[str, FetchResponse]:
    return {START: FetchResponse(200, "text/html", BODY)}


async def _seed(workspace: dict, *, interval: int = 3600,
                now: datetime = T0) -> str:
    engine = create_async_engine(Settings.load().database_url)
    try:
        schedule = await schedule_sync(
            UnitOfWork(engine), EventWriter(), context=_owner_context(workspace),
            collection_id=workspace["collection_id"], name="Docs handbook",
            start_url=START, interval_seconds=interval, now=now,
        )
        return schedule.schedule_id
    finally:
        await engine.dispose()


async def _enqueue(job_type: str, payload: dict, key: str) -> str:
    engine = create_async_engine(Settings.load().database_url)
    try:
        async with engine.begin() as connection:
            return str(await JobQueue().enqueue(
                JobCommand(type=job_type, payload=payload, idempotency_key=key),
                connection,
            ))
    finally:
        await engine.dispose()


def _rows(query: str, values: dict | None = None) -> list:
    async def run() -> list:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                return (await connection.execute(text(query), values or {})).all()
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _execute(query: str, values: dict | None = None) -> None:
    asyncio.run(_execute_async(query, values))


async def _execute_async(query: str, values: dict | None) -> None:
    engine = create_async_engine(Settings.load().database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(query), values or {})
    finally:
        await engine.dispose()


def test_service_refuses_windows_below_the_minimum(db, workspace) -> None:
    async def run() -> str:
        engine = create_async_engine(Settings.load().database_url)
        try:
            with pytest.raises(ValueError, match="at least 300"):
                await schedule_sync(
                    UnitOfWork(engine), EventWriter(),
                    context=_owner_context(workspace),
                    collection_id=workspace["collection_id"], name="Fast",
                    start_url=START, interval_seconds=10, now=T0,
                )
            return "raised"
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == "raised"


def test_due_scan_enqueues_once_per_window(db, workspace) -> None:
    from apps.worker.relay import schedule_due_site_syncs

    async def run() -> tuple[int, int]:
        engine = create_async_engine(Settings.load().database_url)
        try:
            first = await schedule_due_site_syncs(engine)
            second = await schedule_due_site_syncs(engine)
            return first, second
        finally:
            await engine.dispose()

    schedule_id = asyncio.run(_seed(workspace, now=datetime.now(UTC)))
    assert asyncio.run(run()) == (1, 1)  # idempotent: same window, same key
    assert len(_rows(
        "SELECT id FROM jobs WHERE type = 'site_sync' AND payload->>'schedule_id' = :id",
        {"id": schedule_id},
    )) == 1
    assert len(_rows("SELECT id FROM outbox WHERE topic = 'site_sync'")) == 1

    # Not-due schedules are never enqueued (the window is relative to real
    # time: T0 is a fixed date and would age into the past).
    _execute(
        "UPDATE site_sync_schedules SET next_sync_at = :future WHERE id = :id",
        {"future": datetime.now(UTC) + timedelta(hours=2), "id": schedule_id},
    )
    assert asyncio.run(run()) == (0, 0)


def test_sync_actor_stages_and_completes(db, workspace, store_root, monkeypatch) -> None:
    from apps.worker.tasks import sync_site

    schedule_id = asyncio.run(_seed(workspace, now=datetime.now(UTC)))
    job_id = asyncio.run(_enqueue(
        "site_sync", {"schedule_id": schedule_id}, f"site_sync:{schedule_id}:w1",
    ))
    monkeypatch.setattr(sync_site, "_production_crawl", _crawler(_crawler_site()))
    monkeypatch.setattr(sync_site, "ObjectStore", lambda _root: ObjectStore(store_root))

    assert sync_site.site_sync(job_id=job_id, schedule_id=schedule_id) == "staged"
    row = _rows(
        "SELECT last_state, source_id FROM site_sync_schedules WHERE id = :id",
        {"id": schedule_id},
    )[0]
    assert row.last_state == "staged"
    assert row.source_id is not None
    assert _rows(
        "SELECT status FROM jobs WHERE id = :id", {"id": job_id},
    )[0].status == "completed"

    # Redelivery of the same message finds the job completed: skipped.
    assert sync_site.site_sync(job_id=job_id, schedule_id=schedule_id) == "skipped"


def test_sync_actor_fails_job_when_schedule_is_gone(db, store_root, monkeypatch) -> None:
    from apps.worker.tasks import sync_site

    missing = str(uuid.uuid4())
    job_id = asyncio.run(_enqueue(
        "site_sync", {"schedule_id": missing}, "site_sync:gone:window",
    ))
    monkeypatch.setattr(sync_site, "ObjectStore", lambda _root: ObjectStore(store_root))

    assert sync_site.site_sync(job_id=job_id, schedule_id=missing) == "gone"
    assert _rows(
        "SELECT status FROM jobs WHERE id = :id", {"id": job_id},
    )[0].status == "failed"


def test_cutover_actor_retries_until_indexed(db, workspace, store_root, monkeypatch) -> None:
    from apps.worker.tasks import sync_site

    schedule_id = asyncio.run(_seed(workspace, now=datetime.now(UTC)))
    monkeypatch.setattr(sync_site, "_production_crawl", _crawler(_crawler_site()))
    monkeypatch.setattr(sync_site, "ObjectStore", lambda _root: ObjectStore(store_root))
    sync_site.site_sync(
        job_id=asyncio.run(_enqueue(
            "site_sync", {"schedule_id": schedule_id}, f"site_sync:{schedule_id}:w2",
        )),
        schedule_id=schedule_id,
    )

    cutover = _rows("SELECT id, payload FROM jobs WHERE type = 'site_cutover'")[0]
    job_id, version_id = str(cutover.id), cutover.payload["version_id"]

    # Not validated yet: the lease returns to pending for the redelivery.
    with pytest.raises(CutoverPending):
        sync_site.site_cutover(
            job_id=job_id, schedule_id=schedule_id, version_id=version_id,
        )
    assert _rows(
        "SELECT status FROM jobs WHERE id = :id", {"id": job_id},
    )[0].status == "pending"

    # Validate (the extract/index chain itself is proven in test_scheduling).
    _execute(
        "UPDATE source_versions SET state = 'indexed' WHERE id = :id",
        {"id": version_id},
    )
    assert sync_site.site_cutover(
        job_id=job_id, schedule_id=schedule_id, version_id=version_id,
    ) == "synced"
    assert _rows(
        "SELECT status FROM jobs WHERE id = :id", {"id": job_id},
    )[0].status == "completed"
    row = _rows(
        "SELECT last_state, last_synced_at FROM site_sync_schedules WHERE id = :id",
        {"id": schedule_id},
    )[0]
    assert row.last_state == "synced"
    assert row.last_synced_at is not None
    assert _rows(
        "SELECT state FROM source_versions WHERE id = :id", {"id": version_id},
    )[0].state == "active"

    # Redelivery of the completed cutover: skipped.
    assert sync_site.site_cutover(
        job_id=job_id, schedule_id=schedule_id, version_id=version_id,
    ) == "skipped"


def test_production_fetch_offline(db) -> None:
    """The httpx transport honors redirects and feeds the shared crawler."""
    import httpx

    from apps.worker.tasks.sync_site import _production_crawl, _resolve

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs/start":
            return httpx.Response(
                302, headers={"location": "/docs/real"}, content=b"moved"
            )
        return httpx.Response(
            200,
            text="<html><body><p>Vault keys rotate weekly.</p></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    definition = CrawlDefinition(
        "https://docs.example.com/docs/start",
        CrawlPolicy(allowed_hosts=["docs.example.com"], allowed_prefixes=("/docs/",)),
    )

    def client_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    result = _production_crawl(
        definition, client_factory=client_factory, resolve=resolve,
    )
    assert [page.url for page in result.pages] == ["https://docs.example.com/docs/real"]
    assert result.warnings == []
    assert "127.0.0.1" in _resolve("localhost")

    # An oversized response is cut off mid-read; the crawler refuses it.
    def big_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * (11 * 1024 * 1024),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    big = CrawlDefinition(
        "https://docs.example.com/docs/big",
        CrawlPolicy(allowed_hosts=["docs.example.com"], allowed_prefixes=("/docs/",)),
    )
    with pytest.raises(CrawlFailed, match="size limit"):
        _production_crawl(
            big,
            client_factory=lambda **kwargs: httpx.Client(
                transport=httpx.MockTransport(big_handler), **kwargs
            ),
            resolve=resolve,
        )


def test_schedule_services_enforce_ownership_and_state(db, workspace) -> None:
    """Branch coverage for the route-backed service paths, traced directly
    (the TestClient portal thread loses line attribution for these)."""
    from uuid import uuid4

    from identity_access.authorization import AccessContext
    from web_sync.scheduler import (
        ScheduleDisabled,
        ScheduleNotFound,
        request_sync,
        set_schedule_enabled,
    )

    foreign = AccessContext(
        user_id=workspace["owner_id"], workspace_id=str(uuid4()),
        role="owner", collection_ids=[],
    )
    owner = _owner_context(workspace)

    async def run() -> None:
        engine = create_async_engine(Settings.load().database_url)
        uow = UnitOfWork(engine)
        writer = EventWriter()
        try:
            schedule = await schedule_sync(
                uow, writer, context=owner,
                collection_id=workspace["collection_id"], name="Docs handbook",
                start_url=START, interval_seconds=3600, now=T0,
            )
            with pytest.raises(ScheduleNotFound):  # foreign workspace looks unknown
                await request_sync(uow, writer, context=foreign,
                                   schedule_id=schedule.schedule_id, now=T0)
            with pytest.raises(ScheduleNotFound):
                await set_schedule_enabled(uow, writer, context=foreign,
                                           schedule_id=schedule.schedule_id,
                                           enabled=False, now=T0)
            await set_schedule_enabled(uow, writer, context=owner,
                                       schedule_id=schedule.schedule_id,
                                       enabled=False, now=T0)
            with pytest.raises(ScheduleDisabled):
                await request_sync(uow, writer, context=owner,
                                   schedule_id=schedule.schedule_id, now=T0)
            same = await set_schedule_enabled(uow, writer, context=owner,
                                              schedule_id=schedule.schedule_id,
                                              enabled=False, now=T0)
            assert same.enabled is False  # no-change path is a no-op
            resumed = await set_schedule_enabled(uow, writer, context=owner,
                                                 schedule_id=schedule.schedule_id,
                                                 enabled=True, now=T0)
            assert resumed.enabled is True
            requested = await request_sync(uow, writer, context=owner,
                                           schedule_id=schedule.schedule_id, now=T0)
            assert requested.schedule_id == schedule.schedule_id
            with pytest.raises(ScheduleNotFound):
                await request_sync(uow, writer, context=owner,
                                   schedule_id=str(uuid4()), now=T0)
        finally:
            await engine.dispose()

    asyncio.run(run())
