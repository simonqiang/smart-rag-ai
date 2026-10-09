"""Task 16: source version lifecycle over the API — roles, states, 409/404."""

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
OLD_TEXT = b"The approved travel policy allows economy class flights for short trips."


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
                    "sources, collections, invitations, sessions, users, "
                    "workspaces, jobs, outbox CASCADE"
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


def _source(client: TestClient) -> str:
    """A source with one uploaded version (uploads create their own source)."""
    collection = client.post("/api/collections", json={"name": "Policies"}).json()
    uploaded = client.post(
        "/api/uploads",
        files={"file": ("travel.txt", OLD_TEXT)},
        data={"collection_id": collection["collection_id"], "name": "Travel"},
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["source_id"]


def _mark_version(version_id: str, state: str) -> None:
    """Force a version state directly, as the worker pipeline would."""

    async def run() -> None:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE source_versions SET state = :state WHERE id = :id"),
                    {"id": version_id, "state": state},
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _stage_replacement(client: TestClient, source_id: str) -> dict:
    staged = client.post(
        f"/api/sources/{source_id}/versions",
        files={"file": ("travel-v2.txt", b"Revised policy: book the lowest logical fare.")},
    )
    assert staged.status_code == 201, staged.text
    return staged.json()


def test_archive_unarchive_round_trip_and_member_denial(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source(client)

        _invite_and_accept(client)

        archived = client.post(f"/api/sources/{source_id}/archive")
        assert archived.status_code == 200, archived.text
        states = {
            s["source_id"]: s["state"]
            for s in client.get("/api/sources").json()
        }
        assert states[source_id] == "archived"
        unarchived = client.post(f"/api/sources/{source_id}/unarchive")
        assert unarchived.status_code == 200

        _sign_in(client, MEMBER)
        denied = client.post(f"/api/sources/{source_id}/archive")
        assert denied.status_code == 403


def test_version_history_and_replacement_staging(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source(client)
        staged = _stage_replacement(client, source_id)

        assert staged["source_id"] == source_id
        assert staged["source_version_id"]
        history = client.get(f"/api/sources/{source_id}/versions")
        assert history.status_code == 200, history.text
        # No worker in API tests: both versions stay 'uploaded' until processed.
        assert [v["state"] for v in history.json()] == ["uploaded", "uploaded"]


def test_stage_replacement_unknown_source_is_generic_404(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        missing_stage = client.post(
            f"/api/sources/{uuid.uuid4()}/versions",
            files={"file": ("travel-v2.txt", b"content")},
        )
        missing_archive = client.post(f"/api/sources/{uuid.uuid4()}/archive")

    # Unknown and cross-workspace IDs are the identical generic 404.
    assert missing_stage.status_code == missing_archive.status_code == 404
    assert missing_stage.json() == missing_archive.json()


def test_stage_replacement_rejects_unsupported_upload(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source(client)
        rejected = client.post(
            f"/api/sources/{source_id}/versions",
            files={"file": ("malware.exe", b"not an accepted document")},
        )

    assert rejected.status_code == 400
    assert rejected.json() == {
        "detail": "unsupported file type; supported: PDF, TXT, Markdown",
    }


def test_activate_not_ready_returns_409_and_ready_cuts_over(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source(client)
        staged = _stage_replacement(client, source_id)
        version_id = staged["source_version_id"]

        early = client.post(f"/api/sources/{source_id}/versions/{version_id}/activate")
        assert early.status_code == 409, early.text

        _mark_version(version_id, "indexed")  # as the worker pipeline would

        activated = client.post(f"/api/sources/{source_id}/versions/{version_id}/activate")
        assert activated.status_code == 200, activated.text
        history = client.get(f"/api/sources/{source_id}/versions").json()
        states = {v["version_id"]: v["state"] for v in history}
        assert states[version_id] == "active"
        assert list(states.values()).count("active") == 1


def test_rollback_creates_new_version_via_api(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source(client)
        history = client.get(f"/api/sources/{source_id}/versions").json()
        original_version = history[0]["version_id"]
        _mark_version(original_version, "indexed")  # a version that once validated

        rolled_back = client.post(
            f"/api/sources/{source_id}/versions/{original_version}/rollback"
        )
        assert rolled_back.status_code == 201, rolled_back.text
        body = rolled_back.json()
        assert body["source_id"] == source_id
        assert body["version_id"] != original_version
        assert body["state"] == "uploaded"

        after = client.get(f"/api/sources/{source_id}/versions").json()
        assert [v["state"] for v in after] == ["indexed", "uploaded"]


def test_member_cannot_activate_or_rollback(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        source_id = _source(client)
        history = client.get(f"/api/sources/{source_id}/versions").json()
        version_id = history[0]["version_id"]
        _invite_and_accept(client)

        _sign_in(client, MEMBER)
        denied = [
            client.post(f"/api/sources/{source_id}/versions/{version_id}/activate"),
            client.post(f"/api/sources/{source_id}/versions/{version_id}/rollback"),
        ]

    assert all(response.status_code == 403 for response in denied)


def _invite_and_accept(client: TestClient) -> None:
    created = client.post(
        "/api/users/invitations",
        json={"email": MEMBER["email"], "role": "member", "collections": []},
    )
    assert created.status_code == 201, created.text
    acceptance = TestClient(api.app)
    accepted = acceptance.post(
        "/api/invitations/accept",
        json={"token": created.json()["token"], "password": MEMBER["password"]},
    )
    assert accepted.status_code == 201, accepted.text
