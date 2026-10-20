"""Task 22a: operations read models, safe retry, and job detail plumbing.

The display-state derivation (queued/retrying/running/stalled/failed/
completed), the failed-only CAS retry that re-dispatches through the outbox,
and the ``last_error`` workers persist through ``JobClaims``.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.jobs import JobClaims, JobCommand, JobQueue, Lease
from foundation.unit_of_work import UnitOfWork
from operations.job_queries import (
    JobNotFound,
    JobNotRetryable,
    list_jobs,
    list_source_health,
    retry_job,
)

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _run(db: Settings, coro):
    async def run():
        engine = create_async_engine(db.database_url)
        try:
            return await coro(UnitOfWork(engine))
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _job(
    db: Settings,
    *,
    status: str,
    attempts: int = 0,
    job_type: str = "source_extract",
    lease_expired: bool = False,
    last_error: str | None = None,
    created_at: datetime = T0,
) -> str:
    async def seed(uow) -> str:
        job_id = str(uuid.uuid4())
        payload = json.dumps({"job_id": job_id, "version_id": "v1"})
        async with uow.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO jobs (id, type, payload, status, attempts, "
                    "lease_owner, lease_expires_at, last_error, created_at, updated_at) "
                    "VALUES (:id, :type, CAST(:payload AS jsonb), :status, :attempts, "
                    ":lease_owner, :lease_expires_at, :last_error, :created_at, :created_at)"
                ),
                {
                    "id": job_id,
                    "type": job_type,
                    "payload": payload,
                    "status": status,
                    "attempts": attempts,
                    "lease_owner": "token" if status == "running" else None,
                    "lease_expires_at": (
                        datetime.now(UTC) + timedelta(hours=-1 if lease_expired else 1)
                        if status == "running"
                        else None
                    ),
                    "last_error": last_error,
                    "created_at": created_at,
                },
            )
        return job_id

    return _run(db, seed)


def _source_with_versions(
    db: Settings,
    workspace: dict,
    name: str,
    version_states: list[str],
    *,
    source_state: str = "active",
) -> str:
    async def seed(uow) -> str:
        source_id = str(uuid.uuid4())
        async with uow.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, collection_id, name, state, "
                    "created_by, created_at, updated_at) "
                    "VALUES (:id, :workspace_id, :collection_id, :name, :state, "
                    ":created_by, :created_at, :created_at)"
                ),
                {
                    "id": source_id,
                    "workspace_id": workspace["workspace_id"],
                    "collection_id": workspace["collection_id"],
                    "name": name,
                    "state": source_state,
                    "created_by": workspace["owner_id"],
                    "created_at": T0,
                },
            )
            for index, state in enumerate(version_states):
                await connection.execute(
                    text(
                        "INSERT INTO source_versions (id, workspace_id, source_id, "
                        "object_sha256, size_bytes, media_type, filename, state, "
                        "created_by, created_at, updated_at) "
                        "VALUES (:id, :workspace_id, :source_id, :sha, 1, "
                        "'text/plain', :filename, :state, :created_by, "
                        ":created_at, :created_at)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "workspace_id": workspace["workspace_id"],
                        "source_id": source_id,
                        "sha": uuid.uuid4().hex,
                        "filename": f"{name}-v{index}.txt",
                        "state": state,
                        "created_by": workspace["owner_id"],
                        "created_at": T0 + timedelta(minutes=index),
                    },
                )
        return source_id

    return _run(db, seed)


def test_list_jobs_derives_all_display_states(db: Settings) -> None:
    seeded = {
        "queued": _job(db, status="pending", attempts=0),
        "retrying": _job(db, status="pending", attempts=2, last_error="cutover_pending"),
        "running": _job(db, status="running", attempts=1),
        "stalled": _job(db, status="running", attempts=3, lease_expired=True),
        "failed": _job(db, status="failed", attempts=4, last_error="parse_failed"),
        "completed": _job(db, status="completed", attempts=1),
    }
    page = _run(db, lambda uow: list_jobs(uow, limit=50))
    assert page.total == 6
    by_state = {job.state: job for job in page.items}
    assert set(by_state) == set(seeded)
    assert by_state["retrying"].attempts == 2
    assert by_state["retrying"].last_error == "cutover_pending"
    assert by_state["failed"].last_error == "parse_failed"
    assert by_state["failed"].retryable
    assert not by_state["completed"].retryable


def test_list_jobs_filters_by_status_and_type(db: Settings) -> None:
    failed = _job(db, status="failed", last_error="x")
    _job(db, status="completed")
    _job(db, status="failed", job_type="site_sync")

    by_status = _run(db, lambda uow: list_jobs(uow, status="failed"))
    assert by_status.total == 2
    by_type = _run(
        db, lambda uow: list_jobs(uow, status="failed", job_type="source_extract")
    )
    assert by_type.total == 1
    assert by_type.items[0].job_id == failed


def test_list_jobs_paginates(db: Settings) -> None:
    for index in range(3):
        _job(db, status="completed", created_at=T0 + timedelta(minutes=index))
    page = _run(db, lambda uow: list_jobs(uow, limit=2, offset=1))
    assert page.total == 3
    assert len(page.items) == 2  # newest first: index 2, then 1


def test_retry_failed_job_redispatches_via_outbox_and_audits(
    db: Settings, workspace: dict
) -> None:
    job_id = _job(db, status="failed", attempts=1, job_type="source_index")

    retried = _run(
        db,
        lambda uow: retry_job(
            uow, EventWriter(), job_id, actor=workspace["owner_id"]
        ),
    )
    assert retried == job_id

    async def verify(uow):
        async with uow.transaction() as connection:
            job = (
                await connection.execute(
                    text("SELECT status, attempts FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )
            ).mappings().one()
            outbox = (
                await connection.execute(
                    text(
                        "SELECT topic, payload, published_at FROM outbox "
                        "WHERE payload->>'job_id' = :id"
                    ),
                    {"id": job_id},
                )
            ).mappings().all()
            audits = (
                await connection.execute(
                    text(
                        "SELECT type, actor, subject FROM audit_events "
                        "WHERE type = 'operations.job_retried'"
                    )
                )
            ).mappings().all()
        return job, outbox, audits

    job, outbox, audits = _run(db, verify)
    assert job["status"] == "pending"
    assert job["attempts"] == 1  # preserved: the display state is "retrying"
    assert [(row["topic"], row["published_at"] is None) for row in outbox] == [
        ("source_index", True)
    ]
    assert audits[0]["actor"] == workspace["owner_id"]
    assert audits[0]["subject"] == job_id


def test_retry_refuses_unsafe_states_and_unknown_jobs(db: Settings) -> None:
    for status in ("pending", "running", "completed"):
        job_id = _job(db, status=status)
        with pytest.raises(JobNotRetryable):
            _run(
                db,
                lambda uow, j=job_id: retry_job(uow, EventWriter(), j, actor="a"),
            )
    with pytest.raises(JobNotFound):
        _run(
            db,
            lambda uow: retry_job(
                uow, EventWriter(), str(uuid.uuid4()), actor="a"
            ),
        )


def test_double_retry_is_refused(db: Settings) -> None:
    job_id = _job(db, status="failed")
    _run(db, lambda uow: retry_job(uow, EventWriter(), job_id, actor="a"))
    with pytest.raises(JobNotRetryable):
        _run(db, lambda uow: retry_job(uow, EventWriter(), job_id, actor="a"))


def test_claims_persist_typed_detail(db: Settings) -> None:
    async def scenario(uow) -> None:
        queue = JobQueue()
        async with uow.transaction() as connection:
            failed_id = await queue.enqueue(
                JobCommand(type="source_extract", payload={"version_id": "v1"}), connection
            )
            retried_id = await queue.enqueue(
                JobCommand(type="site_cutover", payload={"schedule_id": "s1"}), connection
            )
        claims = JobClaims(uow._engine)

        lease = await claims.claim(failed_id)
        assert isinstance(lease, Lease)
        await claims.fail(lease, detail="parse_failed")

        lease = await claims.claim(retried_id)
        assert isinstance(lease, Lease)
        await claims.retry(lease, detail="cutover_pending")

    _run(db, scenario)

    async def verify(uow):
        async with uow.transaction() as connection:
            return (
                await connection.execute(
                    text("SELECT type, status, last_error FROM jobs ORDER BY type")
                )
            ).mappings().all()

    rows = _run(db, verify)
    by_type = {row["type"]: row for row in rows}
    assert by_type["source_extract"]["status"] == "failed"
    assert by_type["source_extract"]["last_error"] == "parse_failed"
    assert by_type["site_cutover"]["status"] == "pending"
    assert by_type["site_cutover"]["last_error"] == "cutover_pending"


def test_source_health_reports_latest_version_state(db: Settings, workspace: dict) -> None:
    replaced = _source_with_versions(db, workspace, "replaced", ["active", "failed"])
    fresh = _source_with_versions(db, workspace, "fresh", ["uploaded"])
    _source_with_versions(db, workspace, "archived", ["active"], source_state="archived")
    _source_with_versions(db, workspace, "deleted", ["active"], source_state="deleted")

    page = _run(db, lambda uow: list_source_health(uow))
    assert page.total == 3  # deleted tombstone excluded
    by_name = {item.name: item for item in page.items}
    assert by_name["replaced"].latest_version_state == "failed"
    assert by_name["replaced"].source_id == replaced
    assert by_name["fresh"].latest_version_state == "uploaded"
    assert by_name["fresh"].source_id == fresh
    assert by_name["archived"].state == "archived"


def test_pagination_limits_are_clamped(db: Settings) -> None:
    _job(db, status="completed")
    page = _run(db, lambda uow: list_jobs(uow, limit=0, offset=-10))
    assert page.limit == 1
    assert page.offset == 0
    page = _run(db, lambda uow: list_jobs(uow, limit=10_000))
    assert page.limit == 200


def test_source_health_paginates(db: Settings, workspace: dict) -> None:
    for index in range(3):
        _source_with_versions(db, workspace, f"source-{index}", ["uploaded"])
    page = _run(db, lambda uow: list_source_health(uow, limit=2, offset=0))
    assert page.total == 3
    assert len(page.items) == 2
