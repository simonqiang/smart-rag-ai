"""Task 7c: user administration over the API — roles, grants, 404 timing safety."""

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
                text("TRUNCATE invitations, sessions, users, workspaces CASCADE")
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


def _invite(
    client: TestClient,
    email: str,
    role: str,
    collections: list[str] | None = None,
) -> dict:
    response = client.post(
        "/api/users/invitations",
        json={"email": email, "role": role, "collections": collections or []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_member(client: TestClient) -> None:
    token = _invite(client, MEMBER["email"], "member")["token"]
    acceptance = TestClient(api.app)
    response = acceptance.post(
        "/api/invitations/accept", json={"token": token, "password": MEMBER["password"]}
    )
    assert response.status_code == 201, response.text


def _create_admin(client: TestClient) -> None:
    token = _invite(client, ADMIN["email"], "admin")["token"]
    acceptance = TestClient(api.app)
    response = acceptance.post(
        "/api/invitations/accept", json={"token": token, "password": ADMIN["password"]}
    )
    assert response.status_code == 201, response.text


def test_owner_lists_users_with_collections(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _create_member(client)
        listed = client.put(
            f"/api/users/{_member_id(client)}/collections",
            json={"collections": ["col-a", "col-b"]},
        )
        users = client.get("/api/users")

    assert listed.status_code == 204
    body = users.json()
    member = next(user for user in body if user["email"] == MEMBER["email"])
    assert member["role"] == "member"
    assert member["status"] == "active"
    assert member["collections"] == ["col-a", "col-b"]


def _member_id(client: TestClient) -> str:
    users = client.get("/api/users").json()
    return next(user["user_id"] for user in users if user["email"] == MEMBER["email"])


def test_member_cannot_list_or_administer(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _create_member(client)
        _sign_in(client, MEMBER)
        forbidden = [
            client.get("/api/users"),
            client.post(
                "/api/users/invitations",
                json={"email": "x@example.com", "role": "member"},
            ),
            client.post(
                f"/api/users/{workspace['owner_id']}/password-reset",
                json={"new_password": "sneaky-pass-1"},
            ),
            client.post(f"/api/users/{workspace['owner_id']}/disable"),
            client.delete("/api/users/invitations/" + str(uuid.uuid4())),
        ]

    assert all(response.status_code == 403 for response in forbidden), [
        response.status_code for response in forbidden
    ]


def test_admin_invites_member_but_not_admin(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _create_admin(client)
        _sign_in(client, ADMIN)
        allowed = _invite(client, "new-member@example.com", "member", ["col-x"])
        denied = client.post(
            "/api/users/invitations",
            json={"email": "rival-admin@example.com", "role": "admin"},
        )
        invalid = client.post(
            "/api/users/invitations",
            json={"email": "boss@example.com", "role": "owner"},
        )

    assert allowed["invitation_id"] and allowed["token"]
    assert denied.status_code == 403
    assert invalid.status_code == 422


def test_pending_invitations_listed_and_revocable(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        created = _invite(client, "pending@example.com", "member", ["col-a"])
        pending = client.get("/api/users/invitations")
        revoked = client.delete(f"/api/users/invitations/{created['invitation_id']}")
        again = client.delete(f"/api/users/invitations/{created['invitation_id']}")
        unknown = client.delete(f"/api/users/invitations/{uuid.uuid4()}")
        after = client.get("/api/users/invitations")

    assert revoked.status_code == 204
    assert again.status_code == 404
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == again.json()["detail"]
    assert any(
        row["invitation_id"] == created["invitation_id"] for row in pending.json()
    )
    assert pending.json()[0]["collections"] == ["col-a"]
    assert after.json() == []


def test_accept_revoked_invitation_fails(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        created = _invite(client, "gone@example.com", "member")
        client.delete(f"/api/users/invitations/{created['invitation_id']}")

    with TestClient(api.app) as client:
        response = client.post(
            "/api/invitations/accept",
            json={"token": created["token"], "password": "late-pass-1"},
        )

    assert response.status_code == 400


def test_reset_password_role_rules(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _create_member(client)
        _create_admin(client)
        member_id = _member_id(client)
        admin_id = next(
            user["user_id"]
            for user in client.get("/api/users").json()
            if user["email"] == ADMIN["email"]
        )

        # Admin resets member: forced change, old session dies.
        member = TestClient(api.app)
        _sign_in(member, MEMBER)
        reset = client.post(
            f"/api/users/{member_id}/password-reset",
            json={"new_password": "temp-member-pass-1"},
        )
        old_session = member.get("/api/session")

    assert reset.status_code == 204
    assert old_session.status_code == 401

    with TestClient(api.app) as client:
        sign_in = client.post(
            "/api/session", json={"email": MEMBER["email"], "password": "temp-member-pass-1"}
        )
        me = client.get("/api/session")
        # A forced-change session may not use admin routes.
        blocked = client.get("/api/users")
        _sign_in(client, ADMIN)
        admin_reset_admin = client.post(
            f"/api/users/{admin_id}/password-reset",
            json={"new_password": "temp-admin-pass-1"},
        )
        admin_reset_owner = client.post(
            f"/api/users/{workspace['owner_id']}/password-reset",
            json={"new_password": "temp-owner-pass-1"},
        )

    assert sign_in.status_code == 200
    assert me.json()["must_change_password"] is True
    assert blocked.status_code == 403
    assert admin_reset_admin.status_code == 403
    assert admin_reset_owner.status_code == 403


def test_disable_user_revokes_access(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _create_member(client)
        member_id = _member_id(client)
        _sign_in(client, MEMBER)
        self_disable = client.post(f"/api/users/{member_id}/disable")
        _sign_in(client, OWNER)
        disabled = client.post(f"/api/users/{member_id}/disable")

    assert self_disable.status_code == 403
    assert disabled.status_code == 204

    with TestClient(api.app) as client:
        old_session = client.get("/api/session")
        sign_in = client.post("/api/session", json=MEMBER)

    assert old_session.status_code == 401
    assert sign_in.status_code == 401


def test_unknown_and_cross_workspace_targets_identical_404(workspace) -> None:
    foreign_workspace = str(uuid.uuid4())
    foreign_user = str(uuid.uuid4())

    async def seed() -> None:
        from identity_access.passwords import hash_password

        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_workspace},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                    "VALUES (:id, :workspace_id, 'foreign@example.com', :hash, 'owner')"
                ),
                {"id": foreign_user, "workspace_id": foreign_workspace, "hash": hash_password("foreign-pass-1")},
            )
        await engine.dispose()

    asyncio.run(seed())

    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        missing = str(uuid.uuid4())
        unknown = client.post(
            f"/api/users/{missing}/password-reset", json={"new_password": "x-pass-1"}
        )
        foreign = client.post(
            f"/api/users/{foreign_user}/password-reset", json={"new_password": "x-pass-1"}
        )
        unknown_disable = client.post(f"/api/users/{missing}/disable")
        foreign_disable = client.post(f"/api/users/{foreign_user}/disable")
        unknown_collections = client.put(
            f"/api/users/{missing}/collections", json={"collections": ["col-a"]}
        )
        foreign_collections = client.put(
            f"/api/users/{foreign_user}/collections", json={"collections": ["col-a"]}
        )

    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()
    assert unknown_disable.json() == foreign_disable.json()
    assert unknown_collections.json() == foreign_collections.json()
