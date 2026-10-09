"""Permanent source deletion with verified zero residue (Task 17).

Only the owner may request deletion. The request tombstones the source and
enqueues a ``source_delete`` job in one transaction, so retrieval excludes
the source immediately (the ``sources.state`` gate) while the purge itself
runs in the worker. The purge is idempotent — every step rechecks before it
destroys anything — and finishes with a verification receipt: references are
counted again and the ``source.deleted`` audit event with the evidence
commits in the same transaction as the final row deletions. A failed
verification raises ``DeletionIncomplete`` so the job retry reruns the
remaining steps; a clean rerun records zero-count evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ManagedBackupInventory, ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, ProtectedResource, authorize
from source_catalog.catalog import SourceNotFound

DELETION_NOTICE = "[The source cited here has been permanently deleted.]"


@dataclass(frozen=True)
class DeletionEvidence:
    source_id: str
    versions: int
    manifests: int
    vectors: int
    redacted_messages: int
    backups: int
    residue: int


class DeletionIncomplete(Exception):
    """Verification found remaining references; the job must retry."""


async def request_permanent_deletion(
    uow: UnitOfWork, writer: EventWriter, *, context: AccessContext, source_id: str,
) -> None:
    """Tombstone the source and schedule the purge (owner only)."""
    authorize("source.delete", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text("SELECT workspace_id, state FROM sources WHERE id = :id FOR UPDATE"),
                    {"id": source_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None or str(row["workspace_id"]) != context.workspace_id:
            # Unknown and foreign IDs are indistinguishable: IDs cannot be probed.
            raise SourceNotFound(source_id)
        if row["state"] == "deleted":
            return  # idempotent: already tombstoned, the job is scheduled
        await transaction.execute(
            text("UPDATE sources SET state = 'deleted', updated_at = now() WHERE id = :id"),
            {"id": source_id},
        )
        await writer.record(
            AuditEvent(
                type="source.delete_requested",
                actor=context.user_id,
                subject=source_id,
                metadata={},
            ),
            transaction,
        )
        await JobQueue().enqueue(
            JobCommand(
                type="source_delete",
                payload={"source_id": source_id, "workspace_id": context.workspace_id},
                idempotency_key=f"source_delete:{source_id}",
            ),
            transaction,
        )


def _zero_evidence(source_id: str) -> DeletionEvidence:
    return DeletionEvidence(
        source_id=source_id, versions=0, manifests=0, vectors=0,
        redacted_messages=0, backups=0, residue=0,
    )


async def delete_source(
    engine: AsyncEngine,
    store: ObjectStore,
    inventory: ManagedBackupInventory,
    writer: EventWriter,
    *,
    source_id: str,
) -> DeletionEvidence:
    """Purge every trace of a tombstoned source; safe to redeliver."""
    async with engine.begin() as connection:
        source = (
            await connection.execute(
                text("SELECT state FROM sources WHERE id = :id"), {"id": source_id}
            )
        ).mappings().first()
        versions = (
            await connection.execute(
                text(
                    "SELECT id, object_sha256 FROM source_versions "
                    "WHERE source_id = :id ORDER BY created_at"
                ),
                {"id": source_id},
            )
        ).mappings().all()
    if source is None or source["state"] != "deleted":
        # Unknown, never requested, or already fully purged: nothing to do.
        return _zero_evidence(str(source_id))

    backups = inventory.purge_containing(source_id)

    # Manifests are content-addressed: an object shared with another source
    # (duplicate upload) must survive, so delete only unreferenced addresses.
    manifests = 0
    for sha in {str(row["object_sha256"]) for row in versions}:
        async with engine.begin() as connection:
            shared = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM source_versions "
                        "WHERE object_sha256 = :sha AND source_id <> :id"
                    ),
                    {"sha": sha, "id": source_id},
                )
            ).scalar_one()
        if int(shared) == 0:
            try:
                store.delete(sha)
                manifests += 1
            except OSError as failure:
                # Rows are untouched; the redelivery reruns from here.
                raise DeletionIncomplete(
                    f"manifest purge failed for {sha}: {failure}"
                ) from None

    vectors = 0
    redacted = 0
    async with engine.begin() as connection:
        result = await connection.execute(
            text("DELETE FROM index_chunks WHERE source_id = :id"), {"id": source_id}
        )
        vectors = result.rowcount or 0
        await connection.execute(
            text(
                "DELETE FROM index_generations WHERE source_version_id IN "
                "(SELECT id FROM source_versions WHERE source_id = :id)"
            ),
            {"id": source_id},
        )
        result = await connection.execute(
            text(
                "UPDATE conversation_messages SET content = :notice, citations = '[]'::jsonb "
                "WHERE role = 'assistant' AND EXISTS ("
                "  SELECT 1 FROM conversations cv WHERE cv.id = conversation_id"
                "    AND cv.workspace_id = ("
                "      SELECT workspace_id FROM sources WHERE id = :id"
                "    )"
                ") AND EXISTS ("
                "  SELECT 1 FROM jsonb_array_elements(citations) el"
                "  WHERE el->>'source_id' = CAST(:sid AS text)"
                ")"
            ),
            {"id": str(source_id), "sid": str(source_id), "notice": DELETION_NOTICE},
        )
        redacted = result.rowcount or 0

    evidence = DeletionEvidence(
        source_id=str(source_id),
        versions=len(versions),
        manifests=manifests,
        vectors=vectors,
        redacted_messages=redacted,
        backups=backups,
        residue=0,
    )

    async with engine.begin() as connection:
        remaining_chunks = (
            await connection.execute(
                text("SELECT count(*) FROM index_chunks WHERE source_id = :id"),
                {"id": source_id},
            )
        ).scalar_one()
        remaining_generations = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM index_generations WHERE source_version_id IN "
                    "(SELECT id FROM source_versions WHERE source_id = :id)"
                ),
                {"id": source_id},
            )
        ).scalar_one()
        remaining_backups = len(inventory.list_containing(source_id))
        residue = int(remaining_chunks) + int(remaining_generations) + remaining_backups
        if residue:
            raise DeletionIncomplete(
                f"{residue} references remain for source {source_id}"
            )

        await connection.execute(
            text("DELETE FROM source_versions WHERE source_id = :id"), {"id": source_id}
        )
        await connection.execute(
            text("DELETE FROM sources WHERE id = :id AND state = 'deleted'"), {"id": source_id}
        )
        # The tombstone existed when this pass started; record the receipt even
        # if every count is zero (a retry after an earlier partial success).
        await writer.record(
            AuditEvent(
                type="source.deleted",
                actor="system",
                subject=source_id,
                metadata=asdict(evidence),
            ),
            connection,
        )
    return evidence
