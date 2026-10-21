"""Task 17A: retention settings routes — owner-only read and update."""

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
                    "sources, collections, conversations, retention_policies, invitations, "
                    "sessions, users, workspaces, jobs, outbox CASCADE"
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


def test_owner_reads_defaults_and_updates(client, workspace) -> None:
    _sign_in(client, OWNER)

    defaults = client.get("/api/settings/retention")
    assert defaults.status_code == 200, defaults.text
    assert defaults.json() == {
        "superseded_days": 30, "conversation_days": 90, "backup_days": 30,
    }

    updated = client.put(
        "/api/settings/retention",
        json={"superseded_days": 14, "conversation_days": 60, "backup_days": 10},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json() == {
        "superseded_days": 14, "conversation_days": 60, "backup_days": 10,
    }

    assert client.get("/api/settings/retention").json() == {
        "superseded_days": 14, "conversation_days": 60, "backup_days": 10,
    }


def test_only_owners_may_read_or_update(client, workspace) -> None:
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")
    _invite_and_accept(client, MEMBER["email"], MEMBER["password"], "member")

    for person in (ADMIN, MEMBER):
        _sign_in(client, person)
        assert client.get("/api/settings/retention").status_code == 403
        assert client.put(
            "/api/settings/retention",
            json={"superseded_days": 7, "conversation_days": 30, "backup_days": 15},
        ).status_code == 403


def test_non_positive_windows_are_refused(client, workspace) -> None:
    _sign_in(client, OWNER)
    response = client.put(
        "/api/settings/retention",
        json={"superseded_days": 0, "conversation_days": 90, "backup_days": 30},
    )
    assert response.status_code == 422
