"""Rebuild worker (Task 18).

Consumes ``rebuild_index`` jobs. One delivery converges the whole rebuild:
stage every pending member (idempotent, crash redelivery reuses staged
generations), then cut over through ``activate_candidate``'s short final
lock. Persistent per-member refusals (dimension mismatch, empty content)
fail the rebuild without touching the live index; transient provider
outages propagate so Dramatiq retries and the delivery resumes from the
staged generations.

# ponytail: one delivery rebuilds the whole workspace — fine for design-
# partner corpus sizes; split into per-member jobs with lease renewal when
# a rebuild outgrows one worker delivery.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import dramatiq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_providers.contracts import EmbeddingDimensionError, EmbeddingProvider
from ai_providers.ollama import OllamaEmbeddingProvider
from foundation.config import Settings
from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobClaims, Lease
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from knowledge_index.indexer import IndexingError
from knowledge_index.rebuild import (
    activate_candidate,
    rebuild_status,
    stage_candidate,
)

TYPE = "rebuild_index"


@dramatiq.actor(max_retries=3)
def rebuild_index(**payload: str) -> str:
    settings = Settings.load()
    engine = _engine(settings.database_url)
    try:
        return asyncio.run(rebuild_payload(
            engine, ObjectStore(Path(settings.data_dir)), settings.active_profile,
            OllamaEmbeddingProvider(
                settings.ollama_host,
                settings.active_profile.embedding_model,
                settings.active_profile.embedding_dimensions,
            ),
            payload,
        ))
    finally:
        asyncio.run(engine.dispose())


def _engine(database_url: str) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # One asyncio.run per delivery: pooled connections would stay bound to
    # the first (closed) loop and crash on dispose.
    return create_async_engine(database_url, poolclass=NullPool)


async def rebuild_payload(
    engine: AsyncEngine,
    store: ObjectStore,
    profile,
    embeddings: EmbeddingProvider,
    payload: dict,
) -> str:
    claims = JobClaims(engine)
    job_id = uuid.UUID(str(payload["job_id"]))
    lease = await claims.claim(job_id)
    if not isinstance(lease, Lease):  # another consumer holds it, or it is done
        return "skipped"

    uow = UnitOfWork(engine)
    rebuild_id = str(payload["rebuild_id"])
    workspace_id = str(payload["workspace_id"])
    while True:
        status = await rebuild_status(uow, workspace_id=workspace_id)
        if status is None or status["id"] != rebuild_id or status["state"] != "running":
            await claims.complete(lease)
            return "skipped" if status is None else status["state"]
        if status["live"] == status["covered"]:
            # Any member the final drain reveals is already target-covered
            # (the normal pipeline indexes at the current key, and stale-key
            # activations are refused during a rebuild), so convergence here
            # holds; if it ever does not, the raised RebuildNotConverged is
            # redelivered and the next delivery re-derives membership.
            await activate_candidate(
                uow, EventWriter(), workspace_id=workspace_id, rebuild_id=rebuild_id,
            )
            await claims.complete(lease)
            return "completed"
        for version_id in status["missing"]:
            try:
                await stage_candidate(
                    uow, embeddings, store,
                    workspace_id=workspace_id, version_id=version_id, profile=profile,
                )
            except EmbeddingDimensionError:
                return await _fail(engine, rebuild_id, "dimension_mismatch", lease, claims)
            except IndexingError as failure:
                return await _fail(engine, rebuild_id, failure.reason, lease, claims)
        # ``None`` stage results (members that vanished) simply fall through:
        # the next pass re-derives membership.


async def _fail(engine: AsyncEngine, rebuild_id: str, reason: str,
                lease, claims: JobClaims) -> str:
    async with engine.begin() as connection:
        changed = (
            await connection.execute(
                text(
                    "UPDATE index_rebuilds SET state = 'failed', detail = :reason, "
                    "updated_at = now() WHERE id = :id AND state = 'running'"
                ),
                {"reason": reason, "id": rebuild_id},
            )
        ).rowcount
        if changed:
            await EventWriter().record(
                AuditEvent(type="index.rebuild_failed", actor="system",
                           subject=rebuild_id, metadata={"reason": reason}),
                connection,
            )
    await claims.complete(lease)
    return f"failed:{reason}"
