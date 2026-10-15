"""Retention policy and scheduled purge (Task 17A).

The owner sets per-workspace windows (spec §15 defaults: superseded versions
and retired pages 30 days, conversations 90). The scheduled purge reuses the
Task 17 deletion mechanics at version scope: expired ``superseded`` (and, for
Task 20 website snapshots, ``retired``) versions are claimed under a row lock
and rechecked before every destructive step, so a rollback or cutover that
lands between the candidate snapshot and the delete releases the candidate
untouched. Artifacts are content-addressed and shared (duplicate uploads,
rollback reuse), so an object is deleted only when no version row references
it any more and the file predates the cutoff — the age guard also closes the
upload-in-flight race, where an object is stored milliseconds before its row
commits. Every run records a counts-only ``retention.purged`` audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.events import AuditEvent, EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, ProtectedResource, authorize
from retrieval_answering.conversation import delete_expired_conversations

DEFAULT_SUPERSEDED_DAYS = 30
DEFAULT_CONVERSATION_DAYS = 90

# 'retired' is the Task 20 website-page state; retired pages purge through
# the same version lifecycle as superseded uploads.
PURGEABLE_STATES = ("superseded", "retired")

_CANDIDATES_QUERY = text(
    "SELECT id FROM source_versions "
    "WHERE workspace_id = :workspace_id AND state IN :states AND updated_at < :cutoff"
).bindparams(bindparam("states", expanding=True))


@dataclass(frozen=True)
class RetentionPolicy:
    workspace_id: str
    superseded_days: int
    conversation_days: int


@dataclass(frozen=True)
class PurgeEvidence:
    versions: int
    artifacts: int
    conversations: int


class RetentionIncomplete(Exception):
    """An artifact could not be deleted; the job must retry the scan."""


async def get_retention_policy(uow: UnitOfWork, *, workspace_id: str) -> RetentionPolicy:
    """The workspace's windows, or the spec §15 defaults when never edited."""
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text(
                        "SELECT superseded_days, conversation_days FROM retention_policies "
                        "WHERE workspace_id = :workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return RetentionPolicy(
            workspace_id=workspace_id,
            superseded_days=DEFAULT_SUPERSEDED_DAYS,
            conversation_days=DEFAULT_CONVERSATION_DAYS,
        )
    return RetentionPolicy(
        workspace_id=workspace_id,
        superseded_days=int(row["superseded_days"]),
        conversation_days=int(row["conversation_days"]),
    )


async def update_retention_policy(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    superseded_days: int,
    conversation_days: int,
) -> RetentionPolicy:
    """Owner-only upsert of the purge windows; audited transactionally."""
    authorize("retention.update", ProtectedResource(context.workspace_id), context)
    if superseded_days < 1 or conversation_days < 1:
        raise ValueError("retention windows must be at least one day")
    async with uow.transaction() as transaction:
        await transaction.execute(
            text(
                "INSERT INTO retention_policies "
                "(workspace_id, superseded_days, conversation_days, updated_at) "
                "VALUES (:workspace_id, :superseded, :conversation, now()) "
                "ON CONFLICT (workspace_id) DO UPDATE SET "
                "superseded_days = :superseded, conversation_days = :conversation, "
                "updated_at = now()"
            ),
            {
                "workspace_id": context.workspace_id,
                "superseded": superseded_days,
                "conversation": conversation_days,
            },
        )
        await writer.record(
            AuditEvent(
                type="retention.updated",
                actor=context.user_id,
                subject=context.workspace_id,
                metadata={
                    "superseded_days": superseded_days,
                    "conversation_days": conversation_days,
                },
            ),
            transaction,
        )
    return RetentionPolicy(
        workspace_id=context.workspace_id,
        superseded_days=superseded_days,
        conversation_days=conversation_days,
    )


async def purge_version_if_expired(
    engine: AsyncEngine, version_id: str, cutoff: datetime,
) -> bool:
    """Claim one candidate under a row lock and purge it only if it still
    sits in the purgeable window. Returns whether it was purged."""
    async with engine.begin() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT state, updated_at FROM source_versions "
                        "WHERE id = :id FOR UPDATE"
                    ),
                    {"id": version_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        if str(row["state"]) not in PURGEABLE_STATES or row["updated_at"] >= cutoff:
            # The state changed after the candidate snapshot (rollback,
            # cutover, shortened policy): release it without deleting.
            return False
        await connection.execute(
            text("DELETE FROM index_chunks WHERE source_version_id = :id"),
            {"id": version_id},
        )
        await connection.execute(
            text("DELETE FROM index_generations WHERE source_version_id = :id"),
            {"id": version_id},
        )
        await connection.execute(
            text("DELETE FROM source_versions WHERE id = :id"),
            {"id": version_id},
        )
        return True


async def run_retention_purge(
    engine: AsyncEngine, store: ObjectStore, writer: EventWriter,
) -> PurgeEvidence:
    """Idempotent scheduled pass over every workspace; safe to redeliver."""
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        workspaces = [
            str(row[0])
            for row in (await connection.execute(text("SELECT id FROM workspaces"))).all()
        ]
    if not workspaces:
        return PurgeEvidence(versions=0, artifacts=0, conversations=0)

    uow = UnitOfWork(engine)
    policies = {
        workspace: await get_retention_policy(uow, workspace_id=workspace)
        for workspace in workspaces
    }

    versions = conversations = 0
    for workspace, policy in policies.items():
        version_cutoff = now - timedelta(days=policy.superseded_days)
        async with engine.begin() as connection:
            candidates = [
                str(row[0])
                for row in (
                    await connection.execute(
                        _CANDIDATES_QUERY,
                        {
                            "workspace_id": workspace,
                            "states": list(PURGEABLE_STATES),
                            "cutoff": version_cutoff,
                        },
                    )
                ).all()
            ]
        for version_id in candidates:
            if await purge_version_if_expired(engine, version_id, version_cutoff):
                versions += 1
        conversations += await delete_expired_conversations(
            uow, cutoff=now - timedelta(days=policy.conversation_days),
            workspace_id=workspace,
        )

    # Artifacts are content-addressed across workspaces; the most conservative
    # window (oldest cutoff) decides what counts as aged.
    artifact_cutoff = min(
        now - timedelta(days=policy.superseded_days) for policy in policies.values()
    )
    artifacts = await _purge_unreferenced_artifacts(engine, store, artifact_cutoff)
    # Deployments have exactly one workspace (first-owner exclusivity); the
    # run's counts-only receipt names it so Task 22 diagnostics can filter.
    subject = workspaces[0]
    await _record_run(
        engine, writer, subject=subject, versions=versions,
        artifacts=artifacts, conversations=conversations,
    )
    return PurgeEvidence(versions=versions, artifacts=artifacts, conversations=conversations)


async def _purge_unreferenced_artifacts(
    engine: AsyncEngine, store: ObjectStore, cutoff: datetime,
) -> int:
    async with engine.begin() as connection:
        referenced = {
            str(row[0])
            for row in (
                await connection.execute(
                    text("SELECT DISTINCT object_sha256 FROM source_versions")
                )
            ).all()
        }
    deleted = 0
    for address in store.addresses():
        if address in referenced:
            continue
        # ponytail: no mid-scan vanishing guard — the job lease serialises
        # consumers and a redelivery simply reruns the scan.
        if store.age(address) >= cutoff.timestamp():
            continue  # young: a racing upload may still owe its row
        try:
            store.delete(address)
        except OSError as failure:
            # Version rows are already gone; the retry's scan finds the
            # orphan again and finishes the job.
            raise RetentionIncomplete(
                f"artifact purge failed for {address[:12]}: {failure}"
            ) from None
        deleted += 1
    return deleted


async def _record_run(
    engine: AsyncEngine, writer: EventWriter, *, subject: str,
    versions: int, artifacts: int, conversations: int,
) -> None:
    async with engine.begin() as connection:
        await writer.record(
            AuditEvent(
                type="retention.purged",
                actor="system",
                subject=subject,
                metadata={
                    "versions": versions,
                    "artifacts": artifacts,
                    "conversations": conversations,
                    "workspace_id": subject,
                },
            ),
            connection,
        )
