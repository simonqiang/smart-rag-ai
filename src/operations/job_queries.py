"""Operations read models over jobs and source health (Task 22).

Everything here reads state the Task 5 job pipeline already wrote — no new
status transitions, no second dispatch path. ``retry_job`` is the one
mutation: Dramatiq's message is gone by the time a job shows ``failed``, so
a retry flips the row ``failed -> pending`` and inserts a fresh outbox entry
(the relay, not Redis, is what makes a job dispatchable). Every actor is
claim-gated and idempotent, so failed-only is the safe-retry rule; the CAS
predicate makes a double retry a no-op refusal.

Derived display states map the acceptance vocabulary onto the four stored
statuses: queued = ``pending`` never attempted, retrying = ``pending`` after
a worker-initiated retry, stalled = ``running`` past its lease (the
observability hole when Dramatiq exhausts redeliveries), plus failed,
running, and completed verbatim.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Page:
    items: list
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class JobDTO:
    job_id: str
    type: str
    status: str
    state: str  # derived: queued | retrying | running | stalled | failed | completed
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def retryable(self) -> bool:
        return self.status == "failed"


@dataclass(frozen=True)
class SourceHealthDTO:
    source_id: str
    name: str
    state: str
    latest_version_state: str | None
    latest_version_at: datetime | None
    created_at: datetime


_STATE_CASE = (
    "CASE "
    "WHEN status = 'pending' AND attempts = 0 THEN 'queued' "
    "WHEN status = 'pending' THEN 'retrying' "
    "WHEN status = 'running' AND lease_expires_at > now() THEN 'running' "
    "WHEN status = 'running' THEN 'stalled' "
    "ELSE status END"
)


def clamp_page(limit: int, offset: int) -> tuple[int, int]:
    return min(max(limit, 1), MAX_LIMIT), max(offset, 0)


async def list_jobs(
    uow: UnitOfWork,
    *,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Page:
    limit, offset = clamp_page(limit, offset)
    clauses = []
    params: dict = {"limit": limit, "offset": offset}
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    if job_type is not None:
        clauses.append("type = :job_type")
        params["job_type"] = job_type
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with uow.transaction() as connection:
        total = (
            await connection.execute(text(f"SELECT count(*) FROM jobs {where}"), params)
        ).scalar_one()
        rows = (
            await connection.execute(
                text(
                    f"SELECT id, type, status, {_STATE_CASE} AS state, attempts, "
                    "last_error, created_at, updated_at "
                    f"FROM jobs {where} "
                    "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).all()
    return Page(
        items=[
            JobDTO(
                job_id=str(row.id),
                type=row.type,
                status=row.status,
                state=row.state,
                attempts=row.attempts,
                last_error=row.last_error,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


class JobNotFound(Exception):
    """No job with that id exists."""


class JobNotRetryable(Exception):
    """Only failed jobs may be retried."""


async def retry_job(
    uow: UnitOfWork, events: EventWriter, job_id: str, *, actor: str
) -> str:
    """Flip a failed job back to pending and re-dispatch it via the outbox.

    One transaction: the CAS predicate refuses anything not ``failed``, so a
    concurrent retry (or a job that started again meanwhile) is a refusal,
    never a double dispatch. The outbox payload is the original one, which
    already carries ``job_id``.
    """
    async with uow.transaction() as connection:
        existing = (
            await connection.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
            )
        ).first()
        if existing is None:
            raise JobNotFound(job_id)
        row = (
            await connection.execute(
                text(
                    "UPDATE jobs SET status = 'pending', lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = now() "
                    "WHERE id = :id AND status = 'failed' "
                    "RETURNING type, payload"
                ),
                {"id": job_id},
            )
        ).first()
        if row is None:
            raise JobNotRetryable(job_id)
        await connection.execute(
            text(
                "INSERT INTO outbox (id, topic, payload, created_at) "
                "VALUES (:id, :topic, CAST(:payload AS jsonb), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "topic": row.type,
                "payload": json.dumps(row.payload),
            },
        )
        await events.record(
            AuditEvent(
                type="operations.job_retried",
                actor=actor,
                subject=job_id,
                metadata={"job_type": row.type},
            ),
            transaction=connection,
        )
    return job_id


async def list_source_health(
    uow: UnitOfWork, *, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> Page:
    """Live sources with their latest version's state (deleted tombstones excluded)."""
    limit, offset = clamp_page(limit, offset)
    async with uow.transaction() as connection:
        total = (
            await connection.execute(
                text("SELECT count(*) FROM sources WHERE state != 'deleted'")
            )
        ).scalar_one()
        rows = (
            await connection.execute(
                text(
                    "SELECT s.id, s.name, s.state, s.created_at, "
                    "v.state AS version_state, v.created_at AS version_at "
                    "FROM sources s "
                    "LEFT JOIN LATERAL ("
                    "  SELECT state, created_at FROM source_versions v "
                    "  WHERE v.source_id = s.id "
                    "  ORDER BY v.created_at DESC, v.id DESC LIMIT 1"
                    ") v ON true "
                    "WHERE s.state != 'deleted' "
                    "ORDER BY s.created_at DESC, s.id DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            )
        ).all()
    return Page(
        items=[
            SourceHealthDTO(
                source_id=str(row.id),
                name=row.name,
                state=row.state,
                latest_version_state=row.version_state,
                latest_version_at=row.version_at,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
