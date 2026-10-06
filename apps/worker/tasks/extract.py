"""Extraction worker (Task 11).

Consumes ``source_extract`` jobs (recorded by ``JobQueue`` and dispatched
only from committed outbox rows). Delivery is at-least-once, so the actor is
idempotent: job claims deduplicate concurrent deliveries, an already
extracted version short-circuits, and a typed ``ExtractionFailed`` flips the
version to ``failed`` and fails the job without consuming a Dramatiq retry.
Unexpected errors propagate — the version stays ``uploaded`` and a later
delivery retries. The source row and any index pointer are never touched, so
failed extraction cannot activate content.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import dramatiq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.config import Settings
from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobClaims, Lease
from foundation.storage import ObjectStore
from ingestion.extraction import ExtractionFailed, SourceObject, extract

TYPE = "source_extract"


@dramatiq.actor(max_retries=3)
def source_extract(**payload: str) -> str:
    settings = Settings.load()
    engine = _engine(settings.database_url)
    try:
        return asyncio.run(
            extract_version(engine, ObjectStore(Path(settings.data_dir)), payload)
        )
    finally:
        asyncio.run(engine.dispose())


def _engine(database_url: str) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(database_url)


async def extract_version(engine: AsyncEngine, store: ObjectStore, payload: dict) -> str:
    claims = JobClaims(engine)
    job_id = uuid.UUID(str(payload["job_id"]))
    lease = await claims.claim(job_id)
    if not isinstance(lease, Lease):  # another consumer holds it, or it is done
        return "skipped"

    async with engine.begin() as connection:
        version = (
            await connection.execute(
                text(
                    "SELECT state, object_sha256, media_type, filename "
                    "FROM source_versions WHERE id = :id"
                ),
                {"id": payload["version_id"]},
            )
        ).mappings().first()
    if version is None:
        await claims.fail(lease)
        return "missing_version"
    if version["state"] == "extracted":
        await claims.complete(lease)
        return "already_extracted"

    try:
        document = extract(SourceObject(
            source_version_id=payload["version_id"],
            media_type=version["media_type"],
            filename=version["filename"],
            data=store.get(version["object_sha256"]),
        ))
    except ExtractionFailed as failure:
        await _set_version_state(
            engine, payload["version_id"], "failed",
            {"reason": failure.reason}, "source.extraction_failed",
        )
        await claims.fail(lease)
        return f"failed:{failure.reason}"

    if await _job_status(engine, job_id) == "cancelled":
        # Work is discardable until the state flip commits; a cancelled job
        # must never leave extracted content behind.
        return "cancelled"

    await _set_version_state(
        engine, payload["version_id"], "extracted",
        {"blocks": len(document.blocks), "warnings": document.warnings},
        "source.extracted",
    )
    await claims.complete(lease)
    return "extracted"


async def _set_version_state(
    engine: AsyncEngine, version_id: str, state: str, metadata: dict, audit_type: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE source_versions SET state = :state, updated_at = now() "
                "WHERE id = :id"
            ),
            {"state": state, "id": version_id},
        )
        await EventWriter().record(
            AuditEvent(type=audit_type, actor="system", subject=version_id,
                       metadata=metadata),
            connection,
        )


async def _job_status(engine: AsyncEngine, job_id: uuid.UUID) -> str | None:
    async with engine.begin() as connection:
        return (
            await connection.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": str(job_id)}
            )
        ).scalar_one_or_none()
