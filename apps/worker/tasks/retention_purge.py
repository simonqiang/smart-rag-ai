"""Retention purge worker (Task 17A).

Consumes the daily ``retention_purge`` job the relay enqueues from a
committed outbox row. The purge itself is idempotent (see
``source_catalog.retention``); a ``RetentionIncomplete`` artifact failure
releases the job for Dramatiq retry, and the next delivery's scan finishes
the leftover.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import dramatiq
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.jobs import JobClaims, Lease
from foundation.storage import ObjectStore
from source_catalog.retention import RetentionIncomplete, run_retention_purge

TYPE = "retention_purge"


@dramatiq.actor(max_retries=3)
def retention_purge(**payload: str) -> str:
    settings = Settings.load()
    engine = _engine(settings.database_url)
    try:
        return asyncio.run(purge_payload(
            engine, ObjectStore(Path(settings.data_dir)), payload,
        ))
    finally:
        asyncio.run(engine.dispose())


def _engine(database_url: str) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # One asyncio.run per delivery: pooled connections would stay bound to
    # the first (closed) loop and crash on dispose.
    return create_async_engine(database_url, poolclass=NullPool)


async def purge_payload(engine: AsyncEngine, store: ObjectStore, payload: dict) -> str:
    claims = JobClaims(engine)
    job_id = uuid.UUID(str(payload["job_id"]))
    lease = await claims.claim(job_id)
    if not isinstance(lease, Lease):  # another consumer holds it, or it is done
        return "skipped"
    try:
        evidence = await run_retention_purge(engine, store, EventWriter())
    except RetentionIncomplete:
        await claims.retry(lease, detail="retention_incomplete")
        raise
    await claims.complete(lease)
    return f"purged:{evidence.versions}"
