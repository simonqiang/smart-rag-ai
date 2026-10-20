"""Audit event read model (Task 22).

Reads the immutable ``audit_events`` rows written through ``EventWriter``
since Task 5 — the same rows every service already records, no second
writing path. Metadata is JSONB and was sanitized at write time (spec §9:
no prompts, passages, or secrets in audit payloads), so it passes through
untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from foundation.unit_of_work import UnitOfWork
from operations.job_queries import DEFAULT_LIMIT, Page, clamp_page


@dataclass(frozen=True)
class AuditEntryDTO:
    event_id: str
    type: str
    actor: str
    subject: str
    metadata: dict
    created_at: datetime


async def list_audit_events(
    uow: UnitOfWork,
    *,
    event_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Page:
    limit, offset = clamp_page(limit, offset)
    where = "WHERE type = :event_type" if event_type is not None else ""
    params: dict = {"limit": limit, "offset": offset}
    if event_type is not None:
        params["event_type"] = event_type
    async with uow.transaction() as connection:
        total = (
            await connection.execute(
                text(f"SELECT count(*) FROM audit_events {where}"), params
            )
        ).scalar_one()
        rows = (
            await connection.execute(
                text(
                    "SELECT id, type, actor, subject, metadata, created_at "
                    f"FROM audit_events {where} "
                    "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).all()
    return Page(
        items=[
            AuditEntryDTO(
                event_id=str(row.id),
                type=row.type,
                actor=row.actor,
                subject=row.subject,
                metadata=row.metadata or {},
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
