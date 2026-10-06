"""Task 8b: source catalog over the API — grants, direct-ID denial, roles."""

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
ADMIN = {"email": "admin@example.com", "password": "admin-password-1"}


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
                    "TRUNCATE sources, collections, invitations, sessions, "
                    "users, workspaces CASCADE"
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


def _invite_and_accept(client: TestClient, person: dict, role: str, collections: list[str]) -> None:
    created = client.post(
        "/api/users/invitations",
        json={"email": person["email"], "role": role, "collections": collections},
    )
    assert created.status_code == 201, created.text
    acceptance = TestClient(api.app)
    accepted = acceptance.post(
        "/api/invitations/accept",
        json={"token": created.json()["token"], "password": person["password"]},
    )
    assert accepted.status_code == 201, accepted.text


def _collection_id(client: TestClient, name: str) -> str:
    listed = client.get("/api/collections").json()
    return next(c["collection_id"] for c in listed if c["name"] == name)


def test_owner_creates_collection_and_source_then_lists(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        first = client.post("/api/collections", json={"name": "Policies"})
        second = client.post("/api/collections", json={"name": "Policies"})
        assert first.status_code == 201
        assert second.json()["collection_id"] == first.json()["collection_id"]

        created = client.post(
            "/api/sources",
            json={"collection_id": first.json()["collection_id"], "name": "Handbook"},
        )
        assert created.status_code == 201, created.text
        source_id = created.json()["source_id"]

        listed = client.get("/api/sources")
        fetched = client.get(f"/api/sources/{source_id}")

    body = listed.json()
    assert [s["name"] for s in body] == ["Handbook"]
    assert body[0]["state"] == "active"
    assert fetched.status_code == 200
    assert fetched.json()["source_id"] == source_id


def test_member_sees_only_granted_sources(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        shared = client.post("/api/collections", json={"name": "Shared"}).json()["collection_id"]
        private = client.post("/api/collections", json={"name": "Private"}).json()["collection_id"]
        client.post("/api/sources", json={"collection_id": shared, "name": "Handbook"})
        private_source = _source_id_in(client, private)
        client.put(
            f"/api/users/{_member_id(client, create=True)}/collections",
            json={"collections": [shared]},
        )
        _sign_in(client, MEMBER)
        listed = client.get("/api/sources")
        collections = client.get("/api/collections")
        denied = client.get(f"/api/sources/{private_source}")

    assert [s["name"] for s in listed.json()] == ["Handbook"]
    assert [c["name"] for c in collections.json()] == ["Shared"]
    assert denied.status_code == 404


def _member_id(client: TestClient, create: bool) -> str:
    if create:
        _invite_and_accept(client, MEMBER, "member", [])
    users = client.get("/api/users").json()
    return next(u["user_id"] for u in users if u["email"] == MEMBER["email"])


def _source_id_in(client: TestClient, collection_id: str) -> str:
    created = client.post(
        "/api/sources", json={"collection_id": collection_id, "name": "Marker"}
    )
    assert created.status_code == 201
    return created.json()["source_id"]


def test_member_cannot_create_sources_or_collections(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _invite_and_accept(client, MEMBER, "member", [])
        _sign_in(client, MEMBER)
        denied = [
            client.post("/api/collections", json={"name": "Sneaky"}),
            client.post("/api/sources", json={"collection_id": str(uuid.uuid4()), "name": "Sneaky"}),
        ]

    assert all(response.status_code == 403 for response in denied)


def test_admin_can_create_collection_and_source(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _invite_and_accept(client, ADMIN, "admin", [])
        _sign_in(client, ADMIN)
        collection = client.post("/api/collections", json={"name": "Admin area"})
        source = client.post(
            "/api/sources",
            json={"collection_id": collection.json()["collection_id"], "name": "Runbook"},
        )

    assert collection.status_code == 201
    assert source.status_code == 201


def test_unknown_and_foreign_targets_identical_404(workspace) -> None:
    foreign_ws = str(uuid.uuid4())
    foreign_source = str(uuid.uuid4())
    foreign_collection = str(uuid.uuid4())

    async def seed() -> None:
        from identity_access.passwords import hash_password

        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_ws},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                    "VALUES (:id, :workspace_id, 'f@example.com', :hash, 'owner')"
                ),
                {"id": str(uuid.uuid4()), "workspace_id": foreign_ws, "hash": hash_password("f-pass-1")},
            )
            await connection.execute(
                text(
                    "INSERT INTO collections (id, workspace_id, name) "
                    "VALUES (:id, :workspace_id, 'Foreign collection')"
                ),
                {"id": foreign_collection, "workspace_id": foreign_ws},
            )
            await connection.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, collection_id, name, created_by) "
                    "SELECT :sid, :ws, :cid, 'Foreign source', id FROM users "
                    "WHERE workspace_id = :ws LIMIT 1"
                ),
                {"sid": foreign_source, "ws": foreign_ws, "cid": foreign_collection},
            )
        await engine.dispose()

    asyncio.run(seed())

    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        unknown_source, foreign_get = (
            client.get(f"/api/sources/{uuid.uuid4()}"),
            client.get(f"/api/sources/{foreign_source}"),
        )
        unknown_collection_post = client.post(
            "/api/sources", json={"collection_id": str(uuid.uuid4()), "name": "X"}
        )
        foreign_post = client.post(
            "/api/sources", json={"collection_id": foreign_collection, "name": "X"}
        )

    assert unknown_source.status_code == foreign_get.status_code == 404
    assert unknown_source.json() == foreign_get.json()
    assert unknown_collection_post.status_code == foreign_post.status_code == 404
    assert unknown_collection_post.json() == foreign_post.json()


def test_forced_password_change_blocks_catalog_reads(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _invite_and_accept(client, MEMBER, "member", [])
        member_id = _member_id(client, create=False)
        client.post(
            f"/api/users/{member_id}/password-reset",
            json={"new_password": "temp-member-pass-1"},
        )

    with TestClient(api.app) as client:
        sign_in = client.post(
            "/api/session", json={"email": MEMBER["email"], "password": "temp-member-pass-1"}
        )
        blocked = client.get("/api/sources")

    assert sign_in.status_code == 200
    assert blocked.status_code == 403
