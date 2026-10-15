"""Task 18: index generation rebuild routes — start, status, and roles."""

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
                    "TRUNCATE index_chunks, index_generations, index_rebuilds, source_versions, "
                    "sources, collections, invitations, sessions, users, workspaces, "
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
        "/api/users/invitations",
        json={"email": email, "role": role, "collections": []},
    )
    assert invitation.status_code == 201, invitation.text
    client.post("/api/session/logout")
    accepted = client.post(
        "/api/invitations/accept",
        json={"token": invitation.json()["token"], "password": password},
    )
    assert accepted.status_code in (200, 201), accepted.text
    client.post("/api/session/logout")


def test_owner_starts_and_tracks_a_rebuild(client, workspace) -> None:
    _sign_in(client, OWNER)

    empty = client.get("/api/index/rebuild")
    assert empty.status_code == 200
    assert empty.json() == {"state": "idle"}

    started = client.post("/api/index/rebuild")
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["state"] == "running"
    assert body["target_key"]

    status = client.get("/api/index/rebuild")
    assert status.status_code == 200
    assert status.json()["id"] == body["id"]
    assert status.json()["state"] == "running"

    conflict = client.post("/api/index/rebuild")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "rebuild already running"


def test_members_and_admins_cannot_start_a_rebuild(client, workspace) -> None:
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")
    _invite_and_accept(client, MEMBER["email"], MEMBER["password"], "member")

    _sign_in(client, ADMIN)
    assert client.post("/api/index/rebuild").status_code == 403
    assert client.get("/api/index/rebuild").status_code == 200  # admin may observe
    _sign_in(client, MEMBER)
    assert client.post("/api/index/rebuild").status_code == 403
    assert client.get("/api/index/rebuild").status_code == 403
