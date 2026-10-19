"""Site sync scheduling and freshness (Task 21).

``schedule_sync`` registers a website with a per-schedule interval and
dispatches the first sync window from a committed outbox row.
``run_sync`` is the idempotent, clock-controlled worker body: a due window
is claimed by bumping ``next_sync_at`` (CAS on the row), so a redelivered
job in the same window is ``not_due``. An unchanged bundle sha reuses the
stored artifact; a changed one stages a replacement version and dispatches
the standard extract/index chain plus a ``site_cutover`` job — removed
pages leave retrieval exactly when that cutover activates the complete
indexed snapshot, never before. Failed or partial crawls record the detail
and leave the prior snapshot live.

# ponytail: one sync at a time per schedule (the claimed window), no
# claimed_until column — a crashed run skips one window and the next one
# self-heals. A manual run colliding with the automatic window would
# double-crawl; add an in-flight lease only if that is ever observed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, ProtectedResource, authorize
from ingestion.parsers.snapshot import SNAPSHOT_MEDIA_TYPE
from source_catalog.catalog import CollectionNotFound
from source_catalog.version_service import activate_ready_version
from web_sync.crawler import CrawlDefinition, CrawlFailed, CrawlPolicy, CrawlResult
from web_sync.snapshots import _filename, build_snapshot

MIN_INTERVAL_SECONDS = 300
CUTOVER_JOB = "site_cutover"
SYNC_JOB = "site_sync"


class ScheduleNotFound(Exception):
    """Unknown schedule ID; callers map this to the generic 404."""


class ScheduleDisabled(Exception):
    """Run-now was requested for a disabled schedule."""


class CutoverPending(Exception):
    """The replacement is not validated yet; the delivery should retry."""


@dataclass(frozen=True)
class SyncSchedule:
    schedule_id: str
    workspace_id: str
    collection_id: str
    source_id: str | None
    name: str
    start_url: str
    sitemap_url: str | None
    allowed_prefixes: list[str]
    interval_seconds: int
    enabled: bool
    next_sync_at: datetime
    last_synced_at: datetime | None
    last_state: str | None
    last_detail: str | None
    creator_id: str


_FIELDS = (
    "id, workspace_id, collection_id, source_id, name, start_url, sitemap_url, "
    "allowed_prefixes, interval_seconds, enabled, next_sync_at, last_synced_at, "
    "last_state, last_detail, created_by"
)


def _schedule(row) -> SyncSchedule:
    return SyncSchedule(
        schedule_id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        collection_id=str(row["collection_id"]),
        source_id=str(row["source_id"]) if row["source_id"] else None,
        name=row["name"],
        start_url=row["start_url"],
        sitemap_url=row["sitemap_url"],
        allowed_prefixes=list(row["allowed_prefixes"] or []),
        interval_seconds=int(row["interval_seconds"]),
        enabled=bool(row["enabled"]),
        next_sync_at=row["next_sync_at"],
        last_synced_at=row["last_synced_at"],
        last_state=row["last_state"],
        last_detail=row["last_detail"],
        creator_id=str(row["created_by"]),
    )


async def _load(engine: AsyncEngine, schedule_id: str) -> SyncSchedule | None:
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                text(f"SELECT {_FIELDS} FROM site_sync_schedules WHERE id = :id"),
                {"id": schedule_id},
            )
        ).mappings().first()
    return _schedule(row) if row else None


def _definition(schedule: SyncSchedule) -> CrawlDefinition:
    host = (urlsplit(schedule.start_url).hostname or "").lower()
    return CrawlDefinition(
        schedule.sitemap_url or schedule.start_url,
        CrawlPolicy(
            allowed_hosts=[host],
            allowed_prefixes=schedule.allowed_prefixes or None,
        ),
    )


async def schedule_sync(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    collection_id: str,
    name: str,
    start_url: str,
    sitemap_url: str | None = None,
    allowed_prefixes: list[str] | None = None,
    interval_seconds: int = 86_400,
    now: datetime,
) -> SyncSchedule:
    """Register a website for scheduled synchronization (owner/admin)."""
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS} seconds")
    schedule_id = str(uuid.uuid4())
    prefixes = allowed_prefixes or []
    async with uow.transaction() as transaction:
        collection = (
            await transaction.execute(
                text("SELECT workspace_id FROM collections WHERE id = :id"),
                {"id": collection_id},
            )
        ).scalar_one_or_none()
        if collection is None:
            raise CollectionNotFound(collection_id)
        if str(collection) != context.workspace_id:
            raise CollectionNotFound(collection_id)
        await transaction.execute(
            text(
                "INSERT INTO site_sync_schedules (id, workspace_id, collection_id, "
                "name, start_url, sitemap_url, allowed_prefixes, interval_seconds, "
                "next_sync_at, created_by) VALUES (:id, :workspace_id, :collection_id, "
                ":name, :start_url, :sitemap_url, :prefixes, :interval, :now, :created_by)"
            ),
            {
                "id": schedule_id,
                "workspace_id": context.workspace_id,
                "collection_id": collection_id,
                "name": name,
                "start_url": start_url,
                "sitemap_url": sitemap_url,
                "prefixes": json.dumps(prefixes),
                "interval": interval_seconds,
                "now": now,
                "created_by": context.user_id,
            },
        )
        await writer.record(
            AuditEvent(
                type="site.schedule_created",
                actor=context.user_id,
                subject=schedule_id,
                metadata={"name": name, "start_url": start_url,
                          "collection_id": collection_id,
                          "interval_seconds": interval_seconds},
            ),
            transaction,
        )
        # The first window is due now; dispatch commits with the row.
        await JobQueue().enqueue(
            JobCommand(
                type=SYNC_JOB,
                payload={"schedule_id": schedule_id},
                idempotency_key=f"{SYNC_JOB}:{schedule_id}:{now.isoformat()}",
            ),
            transaction,
        )
    return SyncSchedule(
        schedule_id=schedule_id,
        workspace_id=context.workspace_id,
        collection_id=collection_id,
        source_id=None,
        name=name,
        start_url=start_url,
        sitemap_url=sitemap_url,
        allowed_prefixes=prefixes,
        interval_seconds=interval_seconds,
        enabled=True,
        next_sync_at=now,
        last_synced_at=None,
        last_state=None,
        last_detail=None,
        creator_id=context.user_id,
    )


async def list_schedules(
    uow: UnitOfWork, *, workspace_id: str, collection_ids: list[str]
) -> list[SyncSchedule]:
    """Freshness list scoped to the caller's permitted collections."""
    if not collection_ids:
        return []
    async with uow.transaction() as connection:
        rows = (
            await connection.execute(
                text(
                    f"SELECT {_FIELDS} FROM site_sync_schedules "
                    "WHERE workspace_id = :ws "
                    "AND collection_id = ANY(CAST(:ids AS uuid[])) ORDER BY name"
                ),
                {"ws": workspace_id, "ids": collection_ids},
            )
        ).mappings().all()
    return [_schedule(row) for row in rows]


async def _owned_schedule(
    transaction, context: AccessContext, schedule_id: str
) -> SyncSchedule:
    """Load the schedule inside a transaction; foreign IDs look unknown."""
    row = (
        await transaction.execute(
            text(f"SELECT {_FIELDS} FROM site_sync_schedules WHERE id = :id FOR UPDATE"),
            {"id": schedule_id},
        )
    ).mappings().first()
    if row is None:
        raise ScheduleNotFound(schedule_id)
    if str(row["workspace_id"]) != context.workspace_id:
        raise ScheduleNotFound(schedule_id)
    return _schedule(row)


async def request_sync(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    schedule_id: str,
    now: datetime,
) -> SyncSchedule:
    """Run-now (owner/admin): dispatch a forced sync job from the outbox."""
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        schedule = await _owned_schedule(transaction, context, schedule_id)
        disabled = not schedule.enabled
        if disabled:
            raise ScheduleDisabled(schedule_id)
        await JobQueue().enqueue(
            JobCommand(
                type=SYNC_JOB,
                payload={"schedule_id": schedule_id, "force": True},
                idempotency_key=f"{SYNC_JOB}:{schedule_id}:manual:{uuid.uuid4()}",
            ),
            transaction,
        )
        await writer.record(
            AuditEvent(
                type="site.sync_requested", actor=context.user_id, subject=schedule_id,
                metadata={},
            ),
            transaction,
        )
    return schedule


async def set_schedule_enabled(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    schedule_id: str,
    enabled: bool,
    now: datetime,
) -> SyncSchedule:
    """Pause or resume the automatic windows (owner/admin)."""
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        schedule = await _owned_schedule(transaction, context, schedule_id)
        changed = schedule.enabled != enabled
        if changed:
            await transaction.execute(
                text(
                    "UPDATE site_sync_schedules SET enabled = :enabled, "
                    "updated_at = now() WHERE id = :id"
                ),
                {"enabled": enabled, "id": schedule_id},
            )
            await writer.record(
                AuditEvent(
                    type="site.schedule_updated", actor=context.user_id,
                    subject=schedule_id, metadata={"enabled": enabled},
                ),
                transaction,
            )
        schedule = await _owned_schedule(transaction, context, schedule_id)
    return schedule


async def run_sync(
    engine: AsyncEngine,
    writer: EventWriter,
    store: ObjectStore,
    *,
    schedule_id: str,
    now: datetime,
    force: bool = False,
    crawl_site,
) -> str:
    """Run one synchronization window; idempotent under redelivery."""
    schedule = await _load(engine, schedule_id)
    if schedule is None:
        raise ScheduleNotFound(schedule_id)
    if not schedule.enabled:
        return "disabled"

    async with engine.begin() as connection:
        claimed = (
            await connection.execute(
                text(
                    "UPDATE site_sync_schedules SET next_sync_at = :next, "
                    "updated_at = now() WHERE id = :id AND enabled "
                    "AND (:force OR next_sync_at <= :now)"
                ),
                {"id": schedule_id, "next": now + timedelta(seconds=schedule.interval_seconds),
                 "now": now, "force": force},
            )
        ).rowcount
    if not claimed:
        return "not_due"

    try:
        result: CrawlResult = crawl_site(_definition(schedule))
    except CrawlFailed as failure:
        await _finish(engine, writer, schedule, now, "failed", "; ".join(failure.warnings))
        return "failed"
    if result.warnings or not result.pages:
        # A partial crawl stages pages that merely timed out as "removed";
        # refuse it and keep the prior snapshot instead.
        detail = "; ".join(result.warnings) or "no pages crawled"
        await _finish(engine, writer, schedule, now, "failed", detail)
        return "failed"

    bundle = build_snapshot(result)
    address = store.put(bundle.data)
    staged = await _stage(engine, writer, schedule, now, bundle, address)
    return staged


async def _stage(
    engine: AsyncEngine,
    writer: EventWriter,
    schedule: SyncSchedule,
    now: datetime,
    bundle,
    address: str,
) -> str:
    checksum = hashlib.sha256(bundle.data).hexdigest()
    filename = _filename(schedule.start_url)
    version_id = str(uuid.uuid4())
    new_source = schedule.source_id is None
    source_id = schedule.source_id or str(uuid.uuid4())
    async with engine.begin() as transaction:
        await transaction.execute(
            text("SELECT id FROM site_sync_schedules WHERE id = :id FOR UPDATE"),
            {"id": schedule.schedule_id},
        )
        if not new_source:
            state = (
                await transaction.execute(
                    text("SELECT state FROM sources WHERE id = :id"), {"id": source_id},
                )
            ).scalar_one_or_none()
            if state != "active":
                # Tombstoned (purge will cascade this schedule away) or
                # archived by an admin; nothing to stage.
                return "blocked"
            active_sha = (
                await transaction.execute(
                    text(
                        "SELECT object_sha256 FROM source_versions "
                        "WHERE source_id = :id AND state = 'active'"
                    ),
                    {"id": source_id},
                )
            ).scalar_one_or_none()
            if active_sha == checksum:
                await _finish_in(
                    transaction, writer, schedule, now, "unchanged",
                    f"{len(bundle.pages)} pages, artifact reused", bump_synced=True,
                )
                return "unchanged"
        if new_source:
            await transaction.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, collection_id, name, "
                    "state, created_by) VALUES (:id, :workspace_id, :collection_id, "
                    ":name, 'active', :created_by)"
                ),
                {"id": source_id, "workspace_id": schedule.workspace_id,
                 "collection_id": schedule.collection_id, "name": schedule.name,
                 "created_by": schedule.creator_id},
            )
            await writer.record(
                AuditEvent(
                    type="source.created", actor="system", subject=source_id,
                    metadata={"name": schedule.name,
                              "collection_id": schedule.collection_id},
                ),
                transaction,
            )
        await transaction.execute(
            text(
                "INSERT INTO source_versions (id, workspace_id, source_id, "
                "object_sha256, size_bytes, media_type, filename, state, created_by) "
                "VALUES (:id, :workspace_id, :source_id, :sha256, :size, :media_type, "
                ":filename, 'uploaded', :created_by)"
            ),
            {"id": version_id, "workspace_id": schedule.workspace_id,
             "source_id": source_id, "sha256": address,
             "size": len(bundle.data), "media_type": SNAPSHOT_MEDIA_TYPE,
             "filename": filename, "created_by": schedule.creator_id},
        )
        await writer.record(
            AuditEvent(
                type="source.uploaded", actor="system", subject=version_id,
                metadata={"source_id": source_id, "sha256": checksum,
                          "size": len(bundle.data), "media_type": SNAPSHOT_MEDIA_TYPE,
                          "filename": filename, "start_url": schedule.start_url,
                          "pages": len(bundle.pages)},
            ),
            transaction,
        )
        queue = JobQueue()
        await queue.enqueue(
            JobCommand(
                type="source_extract",
                payload={"version_id": version_id, "source_id": source_id},
                idempotency_key=f"source_extract:{version_id}",
            ),
            transaction,
        )
        await queue.enqueue(
            JobCommand(
                type=CUTOVER_JOB,
                payload={"schedule_id": schedule.schedule_id, "version_id": version_id},
                idempotency_key=f"{CUTOVER_JOB}:{version_id}",
            ),
            transaction,
        )
        await _finish_in(
            transaction, writer, schedule, now, "staged",
            f"{len(bundle.pages)} pages staged", bump_synced=False,
        )
        if new_source:
            await transaction.execute(
                text("UPDATE site_sync_schedules SET source_id = :sid WHERE id = :id"),
                {"sid": source_id, "id": schedule.schedule_id},
            )
    return "staged"


async def run_cutover(
    engine: AsyncEngine,
    writer: EventWriter,
    *,
    schedule_id: str,
    version_id: str,
    now: datetime,
) -> str:
    """Activate a validated replacement; the removed pages die exactly here."""
    schedule = await _load(engine, schedule_id)
    if schedule is None:
        raise ScheduleNotFound(schedule_id)
    async with engine.begin() as connection:
        state = (
            await connection.execute(
                text(
                    "SELECT state, source_id FROM source_versions WHERE id = :id"
                ),
                {"id": version_id},
            )
        ).mappings().first()
    if state is None:
        raise ScheduleNotFound(version_id)
    if state["state"] in ("uploaded", "extracted"):
        raise CutoverPending(state["state"])
    if state["state"] == "failed":
        await _finish(engine, writer, schedule, now, "failed",
                      "replacement failed; prior snapshot retained")
        return "failed"
    if state["state"] == "indexed":
        # Shared Task 16 cutover: demote/promote commit atomically under the
        # workspace advisory lock, and the activation lands in the rebuild
        # ledger. The system context is scoped to the schedule's workspace.
        await activate_ready_version(
            UnitOfWork(engine), writer,
            context=AccessContext(user_id="system", workspace_id=schedule.workspace_id,
                                  role="owner"),
            source_id=str(state["source_id"]),
            version_id=str(version_id),
        )
    elif state["state"] != "active":
        # 'active' (first-version auto-promotion) is already live; anything
        # else is a state machine violation.
        raise CutoverPending(state["state"])
    await _finish(engine, writer, schedule, now, "synced", "snapshot live", bump_synced=True)
    return "synced"


async def _finish(
    engine: AsyncEngine,
    writer: EventWriter,
    schedule: SyncSchedule,
    now: datetime,
    state: str,
    detail: str,
    bump_synced: bool = False,
) -> None:
    async with engine.begin() as connection:
        await _finish_in(
            connection, writer, schedule, now, state, detail, bump_synced=bump_synced,
        )


async def _finish_in(
    transaction,
    writer: EventWriter,
    schedule: SyncSchedule,
    now: datetime,
    state: str,
    detail: str,
    bump_synced: bool,
) -> None:
    await transaction.execute(
        text(
            "UPDATE site_sync_schedules SET last_state = :state, last_detail = :detail, "
            "last_synced_at = COALESCE(:synced_at, last_synced_at), updated_at = now() "
            "WHERE id = :id"
        ),
        {"state": state, "detail": detail,
         "synced_at": now if bump_synced else None, "id": schedule.schedule_id},
    )
    await writer.record(
        AuditEvent(
            type="site.sync", actor="system", subject=schedule.schedule_id,
            metadata={"outcome": state, "detail": detail[:500],
                      "source_id": schedule.source_id},
        ),
        transaction,
    )
