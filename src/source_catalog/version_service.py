"""Source version lifecycle: archive, replacement cutover, rollback (Task 16).

Version states form a lifecycle: ``uploaded -> extracted -> indexed`` (ready,
awaiting cutover) ``-> active`` (the single retrievable version) ``-> superseded``.
A partial unique index on ``(source_id) WHERE state = 'active'`` makes the
single-active invariant a database guarantee, so concurrent cutovers admit
exactly one winner and no query can observe two live versions of a source.

Every state change records its audit event inside the same transaction, and
replacement uploads dispatch extraction only from the committed outbox row.
Sources are archived (admin action, immediately excluded from retrieval via
the ``sources.state`` gate) rather than deleted; permanent deletion is the
owner-only Task 17 path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, ProtectedResource, authorize
from ingestion.uploads import UploadAccepted, register_upload
from source_catalog.catalog import SourceNotFound

# States whose object content once passed validation, so rollback may reuse it.
_RESTORABLE = frozenset({"indexed", "active", "superseded"})


@dataclass(frozen=True)
class SourceVersionDTO:
    source_id: str
    version_id: str
    state: str
    object_sha256: str
    size_bytes: int
    media_type: str
    filename: str
    created_at: datetime


class VersionNotReady(Exception):
    """The version is not in the state the requested transition requires."""


async def _owned_source(transaction, context: AccessContext, source_id: str) -> dict:
    row = (
        (
            await transaction.execute(
                text("SELECT * FROM sources WHERE id = :id FOR UPDATE"),
                {"id": source_id},
            )
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or str(row["workspace_id"]) != context.workspace_id
        or row["state"] == "deleted"
    ):
        # Unknown, foreign, and tombstoned sources all return the same 404.
        raise SourceNotFound(source_id)
    return dict(row)


async def archive_source(
    uow: UnitOfWork, writer: EventWriter, *, context: AccessContext, source_id: str
) -> None:
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        source = await _owned_source(transaction, context, source_id)
        if source["state"] == "archived":
            return  # idempotent: archiving an archived source changes nothing
        await transaction.execute(
            text("UPDATE sources SET state = 'archived', updated_at = now() WHERE id = :id"),
            {"id": source_id},
        )
        await writer.record(
            AuditEvent(
                type="source.archived",
                actor=context.user_id,
                subject=source_id,
                metadata={},
            ),
            transaction,
        )


async def unarchive_source(
    uow: UnitOfWork, writer: EventWriter, *, context: AccessContext, source_id: str
) -> None:
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        source = await _owned_source(transaction, context, source_id)
        if source["state"] == "active":
            return  # idempotent
        await transaction.execute(
            text("UPDATE sources SET state = 'active', updated_at = now() WHERE id = :id"),
            {"id": source_id},
        )
        await writer.record(
            AuditEvent(
                type="source.unarchived",
                actor=context.user_id,
                subject=source_id,
                metadata={},
            ),
            transaction,
        )


async def stage_replacement(
    uow: UnitOfWork,
    writer: EventWriter,
    store: ObjectStore,
    *,
    context: AccessContext,
    source_id: str,
    filename: str,
    data: bytes,
) -> UploadAccepted:
    """Upload a new immutable version for an existing source.

    The currently active version stays live until ``activate_ready_version``
    cuts over; the replacement travels the normal extract/index pipeline.
    """
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        await _owned_source(transaction, context, source_id)
    return await register_upload(
        uow, writer, store,
        context=context, collection_id="", name="",
        filename=filename, data=data, source_id=source_id,
    )


async def activate_ready_version(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    source_id: str,
    version_id: str,
) -> None:
    """Cut over to a validated ('indexed') replacement, atomically.

    Demoting the previous active version and promoting this one commit in a
    single transaction; the partial unique index turns a lost race into a
    clean ``VersionNotReady`` instead of two live versions.
    """
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        await _owned_source(transaction, context, source_id)
        version = (
            (
                await transaction.execute(
                    text(
                        "SELECT state FROM source_versions "
                        "WHERE id = :version_id AND source_id = :source_id FOR UPDATE"
                    ),
                    {"version_id": version_id, "source_id": source_id},
                )
            )
            .mappings()
            .first()
        )
        if version is None:
            raise SourceNotFound(source_id)
        if version["state"] != "indexed":
            raise VersionNotReady(str(version["state"]))
        await transaction.execute(
            text(
                "UPDATE source_versions SET state = 'superseded', updated_at = now() "
                "WHERE source_id = :source_id AND state = 'active' AND id <> :version_id"
            ),
            {"source_id": source_id, "version_id": version_id},
        )
        await transaction.execute(
            text(
                "UPDATE source_versions SET state = 'active', updated_at = now() "
                "WHERE id = :version_id"
            ),
            {"version_id": version_id},
        )
        await writer.record(
            AuditEvent(
                type="source.version_activated",
                actor=context.user_id,
                subject=version_id,
                metadata={"source_id": source_id},
            ),
            transaction,
        )


async def rollback_as_new_version(
    uow: UnitOfWork, writer: EventWriter, *, context: AccessContext, source_id: str,
    version_id: str,
) -> SourceVersionDTO:
    """Restore a historical version by staging its content as a new version.

    Versions are immutable: rollback never re-activates the old row, it
    creates a new one pointing at the same content-addressed object and sends
    it through the normal extract/index pipeline. The bad version stays live
    until the restored one cuts over.
    """
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    new_version_id = str(uuid.uuid4())
    async with uow.transaction() as transaction:
        await _owned_source(transaction, context, source_id)
        historical = (
            (
                await transaction.execute(
                    text(
                        "SELECT object_sha256, size_bytes, media_type, filename, state "
                        "FROM source_versions WHERE id = :version_id AND source_id = :source_id"
                    ),
                    {"version_id": version_id, "source_id": source_id},
                )
            )
            .mappings()
            .first()
        )
        if historical is None:
            raise SourceNotFound(source_id)
        if historical["state"] not in _RESTORABLE:
            raise VersionNotReady(str(historical["state"]))
        created_at = (
            await transaction.execute(
                text(
                    "INSERT INTO source_versions (id, workspace_id, source_id, object_sha256, "
                    "size_bytes, media_type, filename, state, created_by) "
                    "VALUES (:id, :workspace_id, :source_id, :sha256, :size, :media_type, "
                    ":filename, 'uploaded', :created_by) RETURNING created_at"
                ),
                {
                    "id": new_version_id,
                    "workspace_id": context.workspace_id,
                    "source_id": source_id,
                    "sha256": historical["object_sha256"],
                    "size": historical["size_bytes"],
                    "media_type": historical["media_type"],
                    "filename": historical["filename"],
                    "created_by": context.user_id,
                },
            )
        ).scalar_one()
        await writer.record(
            AuditEvent(
                type="source.rollback_staged",
                actor=context.user_id,
                subject=new_version_id,
                metadata={"source_id": source_id, "restored_from": version_id},
            ),
            transaction,
        )
        # Dispatches only from this committed outbox row, like a fresh upload.
        await JobQueue().enqueue(
            JobCommand(
                type="source_extract",
                payload={"version_id": new_version_id, "source_id": source_id},
                idempotency_key=f"source_extract:{new_version_id}",
            ),
            transaction,
        )
    return SourceVersionDTO(
        source_id=source_id,
        version_id=new_version_id,
        state="uploaded",
        object_sha256=str(historical["object_sha256"]),
        size_bytes=int(historical["size_bytes"]),
        media_type=str(historical["media_type"]),
        filename=str(historical["filename"]),
        created_at=created_at,
    )


async def list_versions(
    uow: UnitOfWork, *, context: AccessContext, source_id: str
) -> list[SourceVersionDTO]:
    async with uow.transaction() as transaction:
        await _owned_source(transaction, context, source_id)
        rows = (
            await transaction.execute(
                text(
                    "SELECT id, state, object_sha256, size_bytes, media_type, filename, "
                    "created_at FROM source_versions WHERE source_id = :source_id "
                    "ORDER BY created_at, id"
                ),
                {"source_id": source_id},
            )
        ).mappings().all()
    return [
        SourceVersionDTO(
            source_id=source_id,
            version_id=str(row["id"]),
            state=str(row["state"]),
            object_sha256=str(row["object_sha256"]),
            size_bytes=int(row["size_bytes"]),
            media_type=str(row["media_type"]),
            filename=str(row["filename"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]
