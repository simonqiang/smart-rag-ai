"""Site sync workers (Task 21b).

``site_sync`` runs one crawl window (the scheduler's clock-controlled body)
and ``site_cutover`` activates a validated replacement; both consume the
jobs the relay and ``stage_snapshot``/``_stage`` enqueue from committed
outbox rows. The production httpx transport plugs in here — the crawler's
``resolve``/``fetch`` seams stay pure so the security tests run offline.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path

import dramatiq
import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.jobs import JobClaims, Lease
from foundation.storage import ObjectStore
from web_sync.crawler import CrawlDefinition, FetchResponse, crawl
from web_sync.scheduler import CutoverPending, ScheduleNotFound, run_cutover, run_sync


def _resolve(host: str) -> list[str]:
    return sorted({
        str(info[4][0])
        for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    })


def _production_crawl(
    definition: CrawlDefinition, *, client_factory=httpx.Client, resolve=_resolve,
):
    limits = definition.policy.limits
    with client_factory(
        follow_redirects=False, timeout=httpx.Timeout(limits.max_seconds)
    ) as client:

        def fetch(url: str) -> FetchResponse:
            # Cap the read at the policy limit: a hostile oversized response
            # must not fill worker memory before the crawler refuses it.
            with client.stream("GET", url) as response:
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(65_536):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limits.max_response_bytes:
                        break
                return FetchResponse(
                    status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    body=b"".join(chunks),
                    location=response.headers.get("location"),
                )

        return crawl(definition, resolve=resolve, fetch=fetch)


def _engine(database_url: str) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # One asyncio.run per delivery: pooled connections would stay bound to
    # the first (closed) loop and crash on dispose.
    return create_async_engine(database_url, poolclass=NullPool)


async def sync_payload(engine: AsyncEngine, store: ObjectStore, payload: dict) -> str:
    claims = JobClaims(engine)
    job_id = uuid.UUID(str(payload["job_id"]))
    lease = await claims.claim(job_id)
    if not isinstance(lease, Lease):  # another consumer holds it, or it is done
        return "skipped"
    try:
        outcome = await run_sync(
            engine, EventWriter(), store,
            schedule_id=str(payload["schedule_id"]),
            now=datetime.now(UTC),
            force=bool(payload.get("force")),
            crawl_site=_production_crawl,
        )
    except ScheduleNotFound:
        # The schedule cascaded away with its source (Task 17 purge);
        # the delivery must not retry a job that can never succeed.
        await claims.fail(lease)
        return "gone"
    await claims.complete(lease)
    return outcome


async def cutover_payload(engine: AsyncEngine, payload: dict) -> str:
    claims = JobClaims(engine)
    job_id = uuid.UUID(str(payload["job_id"]))
    lease = await claims.claim(job_id)
    if not isinstance(lease, Lease):
        return "skipped"
    try:
        outcome = await run_cutover(
            engine, EventWriter(),
            schedule_id=str(payload["schedule_id"]),
            version_id=str(payload["version_id"]),
            now=datetime.now(UTC),
        )
    except CutoverPending:
        await claims.retry(lease)  # redeliver once the chain validates
        raise
    await claims.complete(lease)
    return outcome


@dramatiq.actor(max_retries=3)
def site_sync(**payload) -> str:
    settings = Settings.load()
    engine = _engine(settings.database_url)
    try:
        return asyncio.run(
            sync_payload(engine, ObjectStore(Path(settings.data_dir)), payload)
        )
    finally:
        asyncio.run(engine.dispose())


@dramatiq.actor(max_retries=3)
def site_cutover(**payload) -> str:
    engine = _engine(Settings.load().database_url)
    try:
        return asyncio.run(cutover_payload(engine, payload))
    finally:
        asyncio.run(engine.dispose())
