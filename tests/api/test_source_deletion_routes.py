"""Task 17: permanent deletion over the API — owner-only tombstone."""

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
                    "TRUNCATE conversation_messages, conversations, index_chunks, "
                    "index_generations, source_versions, sources, collections, "
                    "invitations, sessions, users, workspaces, jobs, outbox CASCADE"
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


def _sign_in(client: TestClient, person: dict) -> None:
    response = client.post("/api/session", json=person)
    assert response.status_code == 200, response.text


def _source_with_version(client: TestClient) -> str:
    collection = client.post("/api/collections", json={"name": "Policies"}).json()
    uploaded = client.post(
        "/api/uploads",
        files={"file": ("travel.txt", b"Travel policy: economy class for short trips.")},
        data={"collection_id": collection["collection_id"], "name": "Travel"},
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["source_id"]


def _invite_and_accept(client: TestClient, role: str) -> None:
    created = client.post(
        "/api/users/invitations",
        json={"email": MEMBER["email"], "role": role, "collections": []},
    )
    assert created.status_code == 201, created.text
    acceptance = TestClient(api.app)
    accepted = acceptance.post(
        "/api/invitations/accept",
        json={"token": created.json()["token"], "password": MEMBER["password"]},
    )
    assert accepted.status_code == 201, accepted.text


def test_owner_tombstones_source_and_schedules_purge(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source_with_version(client)

        scheduled = client.delete(f"/api/sources/{source_id}")
        assert scheduled.status_code == 202, scheduled.text
        assert scheduled.json()["deletion"] == "scheduled"

        state = client.get(f"/api/sources/{source_id}")
        assert state.json()["state"] == "deleted"

        # Idempotent: a second request changes nothing.
        again = client.delete(f"/api/sources/{source_id}")
        assert again.status_code == 202


def test_admin_cannot_delete(workspace) -> None:
    """Deletion is owner-only: even an invited admin is denied."""
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source_with_version(client)
        _invite_and_accept(client, "admin")

        _sign_in(client, MEMBER)  # member@example.com holds the admin role
        denied = client.delete(f"/api/sources/{source_id}")
        assert denied.status_code == 403


def test_delete_unknown_source_is_generic_404(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        missing = client.delete(f"/api/sources/{uuid.uuid4()}")
        other_missing = client.get(f"/api/sources/{uuid.uuid4()}")

    assert missing.status_code == other_missing.status_code == 404
    assert missing.json() == other_missing.json()
