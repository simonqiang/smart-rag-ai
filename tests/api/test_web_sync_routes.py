"""Task 21b: website sync routes — create, grant-scoped freshness, run-now,
enable/disable."""

import asyncio

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
GRANTED = {"email": "granted@example.com", "password": "granted-password-1"}
UNGRANTED = {"email": "ungranted@example.com", "password": "ungranted-password-1"}


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


def _invite_and_accept(client: TestClient, email: str, password: str, role: str) -> str:
    """Invite through the API; returns the new user's ID."""
    _sign_in(client, OWNER)
    invitation = client.post(
        "/api/users/invitations", json={"email": email, "role": role, "collections": []},
    )
    assert invitation.status_code == 201, invitation.text
    client.post("/api/session/logout")
    accepted = client.post(
        "/api/invitations/accept",
        json={"token": invitation.json()["token"], "password": password},
    )
    assert accepted.status_code in (200, 201), accepted.text
    client.post("/api/session/logout")
    _sign_in(client, OWNER)
    users = client.get("/api/users").json()
    return next(u["user_id"] for u in users if u["email"] == email)


def _collection(client: TestClient) -> str:
    _sign_in(client, OWNER)
    created = client.post("/api/collections", json={"name": "Handbook"})
    assert created.status_code == 201, created.text
    return created.json()["collection_id"]


def _create(client: TestClient, collection_id: str, **overrides) -> dict:
    body = {
        "collection_id": collection_id,
        "name": "Docs handbook",
        "start_url": "https://docs.example.com/docs/",
        "interval_seconds": 3600,
        **overrides,
    }
    created = client.post("/api/web-sync", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def test_owner_and_admin_create_members_denied(client, workspace) -> None:
    collection_id = _collection(client)
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")

    schedule = _create(client, collection_id)
    assert schedule["name"] == "Docs handbook"
    assert schedule["enabled"] is True
    assert schedule["source_id"] is None
    assert schedule["last_state"] is None
    assert schedule["next_sync_at"] is not None

    _sign_in(client, ADMIN)
    assert client.post(
        "/api/web-sync",
        json={"collection_id": collection_id, "name": "Admin site",
              "start_url": "https://docs.example.com/docs/"},
    ).status_code == 201

    _invite_and_accept(client, GRANTED["email"], GRANTED["password"], "member")
    _sign_in(client, GRANTED)
    assert client.post(
        "/api/web-sync",
        json={"collection_id": collection_id, "name": "Member site",
              "start_url": "https://docs.example.com/docs/"},
    ).status_code == 403

    _sign_in(client, OWNER)
    assert client.post(
        "/api/web-sync",
        json={"collection_id": collection_id, "name": "Bad",
              "start_url": "https://docs.example.com/docs/", "interval_seconds": 10},
    ).status_code == 422
    assert client.post(
        "/api/web-sync",
        json={"collection_id": "00000000-0000-0000-0000-000000000000", "name": "Ghost",
              "start_url": "https://docs.example.com/docs/"},
    ).status_code == 404


def test_freshness_listing_is_grant_scoped(client, workspace) -> None:
    collection_id = _collection(client)
    _create(client, collection_id)

    granted_id = _invite_and_accept(client, GRANTED["email"], GRANTED["password"], "member")
    _invite_and_accept(client, UNGRANTED["email"], UNGRANTED["password"], "member")
    _sign_in(client, OWNER)
    assert client.put(
        f"/api/users/{granted_id}/collections",
        json={"collections": [collection_id]},
    ).status_code == 204
    client.post("/api/session/logout")

    _sign_in(client, OWNER)
    assert len(client.get("/api/web-sync").json()) == 1
    _sign_in(client, GRANTED)
    assert len(client.get("/api/web-sync").json()) == 1
    _sign_in(client, UNGRANTED)
    assert client.get("/api/web-sync").json() == []


def test_run_now_dispatches_force_job(client, workspace) -> None:
    collection_id = _collection(client)
    _invite_and_accept(client, GRANTED["email"], GRANTED["password"], "member")
    schedule_id = _create(client, collection_id)["schedule_id"]

    _sign_in(client, GRANTED)
    assert client.post(f"/api/web-sync/{schedule_id}/run").status_code == 403

    _sign_in(client, OWNER)
    assert client.post(f"/api/web-sync/{schedule_id}/run").status_code == 202
    assert client.post(
        "/api/web-sync/00000000-0000-0000-0000-000000000000/run"
    ).status_code == 404

    async def jobs() -> list[dict]:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT payload FROM jobs WHERE type = 'site_sync' "
                            "AND payload->>'schedule_id' = :id AND payload ? 'force'"
                        ),
                        {"id": schedule_id},
                    )
                ).mappings().all()
        finally:
            await engine.dispose()
        return [dict(row) for row in rows]

    dispatched = asyncio.run(jobs())
    assert len(dispatched) == 1
    assert dispatched[0]["payload"]["force"] is True


def test_run_now_refuses_disabled_schedules(client, workspace) -> None:
    collection_id = _collection(client)
    _invite_and_accept(client, GRANTED["email"], GRANTED["password"], "member")
    schedule_id = _create(client, collection_id)["schedule_id"]

    _sign_in(client, GRANTED)
    assert client.put(
        f"/api/web-sync/{schedule_id}/enabled", json={"enabled": False},
    ).status_code == 403

    _sign_in(client, OWNER)
    disabled = client.put(f"/api/web-sync/{schedule_id}/enabled", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    assert client.post(f"/api/web-sync/{schedule_id}/run").status_code == 409

    reenabled = client.put(f"/api/web-sync/{schedule_id}/enabled", json={"enabled": True})
    assert reenabled.json()["enabled"] is True
    assert client.post(f"/api/web-sync/{schedule_id}/run").status_code == 202

    assert client.put(
        "/api/web-sync/00000000-0000-0000-0000-000000000000/enabled",
        json={"enabled": False},
    ).status_code == 404
