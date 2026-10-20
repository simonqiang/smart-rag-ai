"""Permanent deletion worker (Task 17).

Consumes ``source_delete`` jobs recorded by ``request_permanent_deletion``
and dispatched only from committed outbox rows. The purge itself is
idempotent (see ``source_catalog.deletion``): a ``DeletionIncomplete``
verification failure releases the job for Dramatiq retry, and a clean
delivery records the ``source.deleted`` audit event with its zero-residue
evidence.
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
from operations.backup import DbManagedBackupInventory
from source_catalog.deletion import DeletionIncomplete, delete_source

TYPE = "source_delete"


@dramatiq.actor(max_retries=3)
def source_delete(**payload: str) -> str:
    settings = Settings.load()
    engine = _engine(settings.database_url)
    try:
        return asyncio.run(
            delete_payload(
                engine, ObjectStore(Path(settings.data_dir)), payload,
            )
        )
    finally:
        asyncio.run(engine.dispose())


def _engine(database_url: str) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # One asyncio.run per delivery: pooled connections would stay bound to
    # the first (closed) loop and crash on dispose.
    return create_async_engine(database_url, poolclass=NullPool)


async def delete_payload(engine: AsyncEngine, store: ObjectStore, payload: dict) -> str:
    settings = Settings.load()
    claims = JobClaims(engine)
    job_id = uuid.UUID(str(payload["job_id"]))
    lease = await claims.claim(job_id)
    if not isinstance(lease, Lease):  # another consumer holds it, or it is done
        return "skipped"

    # Real Task 24 inventory: deletion purges managed backups from disk.
    inventory = DbManagedBackupInventory(engine, Path(settings.backup_dir))
    try:
        evidence = await delete_source(
            engine, store, inventory, EventWriter(),
            source_id=str(payload["source_id"]),
        )
    except DeletionIncomplete:
        await claims.retry(lease, detail="deletion_incomplete")
        raise
    await claims.complete(lease)
    return f"deleted:{evidence.versions}"
