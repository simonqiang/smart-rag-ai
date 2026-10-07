"""Indexing worker (Task 12).

Consumes ``source_index`` jobs. Mirrors the extraction actor's discipline:
job claims deduplicate concurrent deliveries, typed refusals flip the
version to ``failed`` without consuming a Dramatiq retry, and transient
provider outages propagate so a later delivery retries. Staging and
activation are separate steps; a completed delivery short-circuits on the
already-indexed version, and a redelivery after a crash between staging and
activation reuses the staged generation.
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
from ingestion.extraction import SourceObject, extract
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import IndexingError, index_version

TYPE = "source_index"


@dramatiq.actor(max_retries=3)
def source_index(**payload: str) -> str:
    settings = Settings.load()
    engine = _engine(settings.database_url)
    try:
        profile = settings.active_profile
        embeddings = OllamaEmbeddingProvider(
            settings.ollama_host, profile.embedding_model, profile.embedding_dimensions
        )
        return asyncio.run(index_payload(
            engine, ObjectStore(Path(settings.data_dir)), profile, embeddings, payload,
        ))
    finally:
        asyncio.run(engine.dispose())


def _engine(database_url: str) -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(database_url)


async def index_payload(
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

    async with engine.begin() as connection:
        version = (
            await connection.execute(
                text(
                    "SELECT state, workspace_id, object_sha256, media_type, filename "
                    "FROM source_versions WHERE id = :id"
                ),
                {"id": payload["version_id"]},
            )
        ).mappings().first()
    if version is None:
        await claims.fail(lease)
        return "missing_version"
    if version["state"] == "indexed":
        await claims.complete(lease)
        return "already_indexed"
    if version["state"] != "extracted":
        await claims.fail(lease)
        return f"not_indexable:{version['state']}"

    try:
        document = extract(SourceObject(
            source_version_id=payload["version_id"],
            media_type=version["media_type"],
            filename=version["filename"],
            data=store.get(version["object_sha256"]),
        ))
        staged = await index_version(
            UnitOfWork(engine), embeddings,
            workspace_id=str(version["workspace_id"]),
            source_version_id=payload["version_id"],
            document=document,
            profile=profile,
        )
    except IndexingError as failure:
        await _fail_version(engine, payload["version_id"], failure.reason)
        await claims.fail(lease)
        return f"failed:{failure.reason}"
    except EmbeddingDimensionError as failure:
        # Persistent provider/profile mismatch: retrying cannot fix it.
        await _fail_version(engine, payload["version_id"], failure.reason)
        await claims.fail(lease)
        return f"failed:{failure.reason}"

    if await _job_status(engine, job_id) == "cancelled":
        # Discardable until the activation pointer commits; the staged
        # generation is inert and retrieval never joins it.
        return "cancelled"
    await activate_generation(
        UnitOfWork(engine), EventWriter(), staged.generation_id,
        workspace_id=str(version["workspace_id"]),
    )
    await claims.complete(lease)
    return "indexed"


async def _fail_version(engine: AsyncEngine, version_id: str, reason: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE source_versions SET state = 'failed', updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": version_id},
        )
        await EventWriter().record(
            AuditEvent(type="source.indexing_failed", actor="system", subject=version_id,
                       metadata={"reason": reason}),
            connection,
        )


async def _job_status(engine: AsyncEngine, job_id: uuid.UUID) -> str | None:
    async with engine.begin() as connection:
        return (
            await connection.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": str(job_id)}
            )
        ).scalar_one_or_none()
