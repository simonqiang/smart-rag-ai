"""Task 24c: managed-backup settings routes — owner-only list and create."""

import asyncio
from datetime import datetime
from pathlib import Path

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
KEY = "correct horse battery staple"


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # The create route reads settings per request: point backup_dir at a
    # scratch directory so the live dev stack's backups stay untouched.
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("SMART_RAG_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("SMART_RAG_BACKUP_KEY", KEY)

    async def create() -> dict:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE index_chunks, index_generations, source_versions, sources, "
                    "collections, conversations, retention_policies, managed_backups, "
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

    created = asyncio.run(create())
    created["backup_dir"] = backup_dir
    return created


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


def test_owner_creates_and_lists_backups(client, workspace) -> None:
    _sign_in(client, OWNER)

    empty = client.get("/api/settings/backup")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"backups": []}

    created = client.post("/api/settings/backup")
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["filename"].endswith(".srb")
    assert (workspace["backup_dir"] / payload["filename"]).exists()
    listed = client.get("/api/settings/backup").json()["backups"]
    assert len(listed) == 1
    row = listed[0]
    assert row["id"] == payload["id"]
    assert row["filename"] == payload["filename"]
    assert row["size_bytes"] == payload["size_bytes"]
    datetime.fromisoformat(row["created_at"])  # the row carries a real timestamp
    assert row["source_count"] == 0
    assert asyncio.run(_audit_count("backup.created")) == 1


async def _audit_count(event_type: str) -> int:
    engine = create_async_engine(Settings.load().database_url)
    try:
        async with engine.begin() as connection:
            return int((
                await connection.execute(
                    text("SELECT count(*) FROM audit_events WHERE type = :t"),
                    {"t": event_type},
                )
            ).scalar_one())
    finally:
        await engine.dispose()


def test_missing_recovery_key_is_an_actionable_400(
    client, workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sign_in(client, OWNER)
    monkeypatch.delenv("SMART_RAG_BACKUP_KEY")
    refused = client.post("/api/settings/backup")
    assert refused.status_code == 400, refused.text
    assert "SMART_RAG_BACKUP_KEY" in refused.json()["detail"]
    assert client.get("/api/settings/backup").json() == {"backups": []}


def test_only_owner_lists_or_creates(client, workspace) -> None:
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")
    _invite_and_accept(client, MEMBER["email"], MEMBER["password"], "member")

    for person in (ADMIN, MEMBER):
        _sign_in(client, person)
        assert client.get("/api/settings/backup").status_code == 403
        assert client.post("/api/settings/backup").status_code == 403
