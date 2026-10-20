"""Task 22a: audit event read model over the Task 5 events."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from operations.audit import list_audit_events

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _run(db: Settings, coro):
    async def run():
        engine = create_async_engine(db.database_url)
        try:
            return await coro(UnitOfWork(engine))
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _event(
    db: Settings,
    *,
    type: str,
    created_at: datetime,
    actor: str = "actor@example.com",
    subject: str = "00000000-0000-0000-0000-000000000000",
    metadata: dict | None = None,
) -> str:
    async def seed(uow) -> str:
        event_id = str(uuid.uuid4())
        async with uow.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO audit_events (id, type, actor, subject, metadata, created_at) "
                    "VALUES (:id, :type, :actor, :subject, "
                    "CAST(:metadata AS jsonb), :created_at)"
                ),
                {
                    "id": event_id,
                    "type": type,
                    "actor": actor,
                    "subject": subject,
                    "metadata": "{}" if metadata is None else json.dumps(metadata),
                    "created_at": created_at,
                },
            )
        return event_id

    return _run(db, seed)


def test_lists_newest_first_with_pagination(db: Settings) -> None:
    first = _event(db, type="source.created", created_at=T0)
    second = _event(db, type="user.invited", created_at=T0 + timedelta(minutes=1))
    third = _event(db, type="source.deleted", created_at=T0 + timedelta(minutes=2))

    page = _run(db, lambda uow: list_audit_events(uow, limit=2))
    assert page.total == 3
    assert [item.event_id for item in page.items] == [third, second]

    next_page = _run(db, lambda uow: list_audit_events(uow, limit=2, offset=2))
    assert [item.event_id for item in next_page.items] == [first]


def test_filters_by_event_type(db: Settings) -> None:
    _event(db, type="source.created", created_at=T0)
    kept = _event(db, type="source.deleted", created_at=T0 + timedelta(minutes=1))

    page = _run(
        db, lambda uow: list_audit_events(uow, event_type="source.deleted")
    )
    assert page.total == 1
    assert page.items[0].event_id == kept


def test_reads_events_written_through_event_writer(db: Settings, workspace: dict) -> None:
    async def write(uow) -> None:
        async with uow.transaction() as connection:
            await EventWriter().record(
                AuditEvent(
                    type="operations.job_retried",
                    actor=workspace["owner_id"],
                    subject="job-1",
                    metadata={"job_type": "source_index"},
                ),
                transaction=connection,
            )

    _run(db, write)
    page = _run(
        db,
        lambda uow: list_audit_events(uow, event_type="operations.job_retried"),
    )
    assert page.total == 1
    entry = page.items[0]
    assert entry.type == "operations.job_retried"
    assert entry.actor == workspace["owner_id"]
    assert entry.subject == "job-1"
    assert entry.metadata == {"job_type": "source_index"}


def test_empty_workspace_yields_empty_page(db: Settings) -> None:
    page = _run(db, lambda uow: list_audit_events(uow))
    assert page.total == 0
    assert page.items == []
