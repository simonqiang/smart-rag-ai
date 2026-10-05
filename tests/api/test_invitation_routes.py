"""Task 6d: invitation acceptance and forced password change over the API."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api import main as api
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.invitations import issue_invitation_token, set_temporary_password
from identity_access.setup import create_first_owner

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def workspace():
    async def create() -> str:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE invitations, sessions, users, workspaces CASCADE")
            )
            await connection.execute(text("TRUNCATE audit_events"))
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), **OWNER)
        finally:
            await engine.dispose()
        return owner.workspace_id

    return asyncio.run(create())


def _invite(role: str = "member", email: str = "member@example.com") -> str:
    async def run() -> str:
        engine = create_async_engine(Settings.load().database_url)
        uow = UnitOfWork(engine)
        try:
            async with engine.connect() as connection:
                workspace_id = (
                    await connection.execute(text("SELECT id FROM workspaces LIMIT 1"))
                ).scalar_one()
                owner_id = (
                    await connection.execute(
                        text("SELECT id FROM users WHERE role = 'owner' LIMIT 1")
                    )
                ).scalar_one()
            invitation = await issue_invitation_token(
                uow,
                EventWriter(),
                workspace_id=str(workspace_id),
                email=email,
                role=role,
                invited_by=str(owner_id),
            )
        finally:
            await engine.dispose()
        return invitation.token

    return asyncio.run(run())


def test_accept_invitation_signs_in_new_member(workspace) -> None:
    token = _invite()
    with TestClient(api.app) as client:
        response = client.post(
            "/api/invitations/accept", json={"token": token, "password": "member-pass-1"}
        )
        me = client.get("/api/session")

    assert response.status_code == 201
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "member@example.com"
    assert body["role"] == "member"
    assert body["workspace_id"] == workspace


def test_accept_invalid_token_is_generic_400(workspace) -> None:
    with TestClient(api.app) as client:
        response = client.post(
            "/api/invitations/accept", json={"token": "bogus", "password": "member-pass-1"}
        )

    assert response.status_code == 400
    assert "invalid" in response.json()["detail"]


def test_temporary_password_forces_change_then_clears(workspace) -> None:
    token = _invite()
    user_id = ""

    with TestClient(api.app) as client:
        client.post("/api/invitations/accept", json={"token": token, "password": "member-pass-1"})
        user_id = client.get("/api/session").json()["user_id"]

    async def make_temporary() -> None:
        engine = create_async_engine(Settings.load().database_url)
        uow = UnitOfWork(engine)
        try:
            await set_temporary_password(
                uow, EventWriter(), user_id=user_id, temp_password="temp-pass-9"
            )
        finally:
            await engine.dispose()

    asyncio.run(make_temporary())

    with TestClient(api.app) as client:
        sign_in = client.post(
            "/api/session", json={"email": "member@example.com", "password": "temp-pass-9"}
        )
        me = client.get("/api/session")
        forced = client.post(
            "/api/session/password",
            json={"current_password": "temp-pass-9", "new_password": "permanent-pass-12"},
        )

    assert sign_in.status_code == 200
    assert me.json()["must_change_password"] is True
    assert forced.status_code == 204

    with TestClient(api.app) as client:
        old_password = client.post(
            "/api/session", json={"email": "member@example.com", "password": "temp-pass-9"}
        )
        new_password = client.post(
            "/api/session", json={"email": "member@example.com", "password": "permanent-pass-12"}
        )
        me = new_password and client.get("/api/session")

    assert old_password.status_code == 401
    assert new_password.status_code == 200
    assert me.json()["must_change_password"] is False


def test_change_password_wrong_current_is_403(workspace) -> None:
    token = _invite()
    with TestClient(api.app) as client:
        client.post("/api/invitations/accept", json={"token": token, "password": "member-pass-1"})
        response = client.post(
            "/api/session/password",
            json={"current_password": "wrong", "new_password": "whatever-pass-1"},
        )

    assert response.status_code == 403
