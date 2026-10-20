"""Task 22b: operations routes — jobs, retry, source health, audit,
diagnostics; owner/admin allowed, members denied."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api import main as api
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.setup import create_first_owner

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}
ADMIN = {"email": "admin@example.com", "password": "admin-password-1"}
MEMBER = {"email": "member@example.com", "password": "member-password-1"}


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def workspace():
    async def create() -> dict:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE index_chunks, index_generations, source_versions, "
                    "site_sync_schedules, sources, collections, conversations, "
                    "retention_policies, invitations, sessions, users, workspaces, "
                    "jobs, outbox CASCADE"
                )
            )
            await connection.execute(text("TRUNCATE audit_events"))
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), **OWNER)
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id, "owner_id": owner.user_id}

    return asyncio.run(create())


@pytest.fixture()
def client():
    return TestClient(api.app)


def _sign_in(client: TestClient, person: dict) -> None:
    response = client.post("/api/session", json=person)
    assert response.status_code == 200, response.text


def _invite_and_accept(client: TestClient, email: str, password: str, role: str) -> None:
    _sign_in(client, OWNER)
    invitation = client.post(
        "/api/users/invitations", json={"email": email, "role": role, "collections": []},
    )
    assert invitation.status_code == 201, invitation.text
    client.post("/api/session/logout")
    accepted = client.post(
        "/api/invitations/accept", json={"token": invitation.json()["token"], "password": password},
    )
    assert accepted.status_code in (200, 201), accepted.text
    client.post("/api/session/logout")


def _job(client: TestClient, *, status: str, attempts: int = 0) -> str:
    async def seed():
        engine = create_async_engine(Settings.load().database_url)
        try:
            job_id = str(uuid.uuid4())
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO jobs (id, type, payload, status, attempts, "
                        "created_at, updated_at) "
                        "VALUES (:id, 'source_extract', CAST(:payload AS jsonb), "
                        ":status, :attempts, now(), now())"
                    ),
                    {
                        "id": job_id,
                        "payload": f'{{"job_id": "{job_id}"}}',
                        "status": status,
                        "attempts": attempts,
                    },
                )
            return job_id
        finally:
            await engine.dispose()

    return asyncio.run(seed())


def test_jobs_listing_states_and_filters(client, workspace) -> None:
    _job(client, status="pending", attempts=0)
    _job(client, status="failed", attempts=2)
    _sign_in(client, OWNER)
    page = client.get("/api/operations/jobs")
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == 2
    states = {item["state"] for item in body["items"]}
    assert states == {"queued", "failed"}

    only_failed = client.get("/api/operations/jobs?status=failed").json()
    assert only_failed["total"] == 1
    item = only_failed["items"][0]
    assert item["state"] == "failed"
    assert item["retryable"] is True


def test_retry_failed_job_ok_and_audited(client, workspace) -> None:
    job_id = _job(client, status="failed")
    _sign_in(client, OWNER)
    response = client.post(f"/api/operations/jobs/{job_id}/retry")
    assert response.status_code == 202, response.text

    async def verify():
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                status = (
                    await connection.execute(
                        text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id}
                    )
                ).scalar_one()
                outbox = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM outbox WHERE payload->>'job_id' = :id "
                            "AND published_at IS NULL"
                        ),
                        {"id": job_id},
                    )
                ).scalar_one()
                audit = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM audit_events "
                            "WHERE type = 'operations.job_retried' AND subject = :id"
                        ),
                        {"id": job_id},
                    )
                ).scalar_one()
            return status, outbox, audit
        finally:
            await engine.dispose()

    status, outbox, audit = asyncio.run(verify())
    assert status == "pending"
    assert outbox == 1
    assert audit == 1


def test_retry_refuses_pending_running_completed_and_unknown(client, workspace) -> None:
    _sign_in(client, OWNER)
    for status in ("pending", "running", "completed"):
        job_id = _job(client, status=status)
        assert client.post(f"/api/operations/jobs/{job_id}/retry").status_code == 409
    assert client.post(
        f"/api/operations/jobs/{uuid.uuid4()}/retry"
    ).status_code == 404


def test_member_denied_on_every_operations_read_and_retry(client, workspace) -> None:
    _invite_and_accept(client, MEMBER["email"], MEMBER["password"], "member")
    job_id = _job(client, status="failed")
    _sign_in(client, MEMBER)
    assert client.get("/api/operations/jobs").status_code == 403
    assert client.get("/api/operations/sources/health").status_code == 403
    assert client.get("/api/operations/audit").status_code == 403
    assert client.get("/api/operations/diagnostics").status_code == 403
    assert client.post(f"/api/operations/jobs/{job_id}/retry").status_code == 403


def test_admin_reads_and_retries(client, workspace) -> None:
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")
    job_id = _job(client, status="failed")
    _sign_in(client, ADMIN)
    assert client.get("/api/operations/jobs").status_code == 200
    assert client.post(f"/api/operations/jobs/{job_id}/retry").status_code == 202


def test_source_health_and_audit_endpoints(client, workspace) -> None:
    _sign_in(client, OWNER)
    collection = client.post("/api/collections", json={"name": "Handbook"}).json()
    # audit already has workspace.initialized + collection events; assert shape.
    audit_page = client.get("/api/operations/audit?limit=5").json()
    assert audit_page["total"] >= 2
    assert {"event_id", "type", "actor", "metadata", "created_at"} <= set(
        audit_page["items"][0]
    )
    health = client.get("/api/operations/sources/health").json()
    assert health["total"] == 0
    assert health["items"] == []
    assert collection["name"] == "Handbook"  # keep the create asserted


def test_diagnostics_reports_redacted_checks(client, workspace) -> None:
    _sign_in(client, OWNER)
    response = client.get("/api/operations/diagnostics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"host", "ok", "checks"} <= set(body)
    for check in body["checks"]:
        assert check["status"] in ("ok", "warn", "fail")
        assert "secret" not in check["detail"].lower() or "[redacted]" in check["detail"]


def test_empty_jobs_page_shape(client, workspace) -> None:
    _sign_in(client, OWNER)
    page = client.get("/api/operations/jobs").json()
    assert page == {"items": [], "total": 0, "limit": 50, "offset": 0}
