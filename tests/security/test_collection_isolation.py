"""Task 7d: collection isolation and direct-ID denial security proofs."""

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
from identity_access.authorization import (
    AccessContext,
    accessible_collection_ids,
    can_access_collection,
)
from identity_access.grants import (
    TargetNotFound,
    disable_user,
    set_user_collections,
)
from identity_access.invitations import issue_invitation_token
from identity_access.setup import create_first_owner

OWNER_EMAIL = "owner@example.com"
MEMBER_EMAIL = "member@example.com"


def _run(settings: Settings, coro_factory) -> object:
    async def run() -> object:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        try:
            return await coro_factory(uow)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def workspace(db: Settings) -> dict:
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            from identity_access.invitations import accept_invitation

            owner = await create_first_owner(uow, EventWriter(), email=OWNER_EMAIL, password="owner-password-1")
            invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id,
                email=MEMBER_EMAIL, role="member", invited_by=owner.user_id,
            )
            accepted = await accept_invitation(
                uow, EventWriter(), token=invite.token, password="member-password-1"
            )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "member_id": accepted.user_id,
        }

    return asyncio.run(create())


def _member_context(workspace: dict, collections: list[str] | None) -> AccessContext:
    return AccessContext(
        user_id=workspace["member_id"],
        workspace_id=workspace["workspace_id"],
        role="member",
        collection_ids=collections,
    )


def test_member_without_grant_cannot_reach_collection_directly(db, workspace) -> None:
    async def probe(uow) -> object:
        grants = await accessible_collection_ids(
            uow, user_id=workspace["member_id"], workspace_id=workspace["workspace_id"]
        )
        context = _member_context(workspace, grants)
        return grants, can_access_collection(context, "col-secret")

    grants, allowed = _run(db, probe)
    assert grants == []
    assert allowed is False


def test_revoked_grant_immediately_loses_access(db, workspace) -> None:
    context = AccessContext(
        user_id=workspace["owner_id"],
        workspace_id=workspace["workspace_id"],
        role="owner",
    )

    async def grant(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=workspace["member_id"], collections=["col-a"],
        )

    _run(db, grant)

    async def access(uow) -> object:
        return await accessible_collection_ids(
            uow, user_id=workspace["member_id"], workspace_id=workspace["workspace_id"]
        )

    assert _run(db, access) == ["col-a"]

    async def revoke(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=workspace["member_id"], collections=[],
        )

    _run(db, revoke)
    assert _run(db, access) == []


def test_cross_workspace_direct_ids_are_denied(db, workspace) -> None:
    foreign_workspace = str(uuid.uuid4())
    foreign_user = str(uuid.uuid4())

    async def seed(uow) -> None:
        from identity_access.passwords import hash_password

        async with uow.transaction() as transaction:
            await transaction.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_workspace},
            )
            await transaction.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                    "VALUES (:id, :workspace_id, 'foreign@example.com', :hash, 'member')"
                ),
                {"id": foreign_user, "workspace_id": foreign_workspace, "hash": hash_password("foreign-pass-1")},
            )

    _run(db, seed)

    async def probe(uow) -> object:
        grants = await accessible_collection_ids(
            uow, user_id=workspace["member_id"], workspace_id=workspace["workspace_id"]
        )
        context = _member_context(workspace, grants)
        return can_access_collection(context, "foreign-collection")

    assert _run(db, probe) is False


def test_unknown_user_resolves_to_no_access(db, workspace) -> None:
    async def probe(uow) -> object:
        return await accessible_collection_ids(
            uow, user_id=str(uuid.uuid4()), workspace_id=workspace["workspace_id"]
        )

    assert _run(db, probe) == []


def test_disabled_user_resolves_to_no_access(db, workspace) -> None:
    context = AccessContext(
        user_id=workspace["owner_id"],
        workspace_id=workspace["workspace_id"],
        role="owner",
    )

    async def grant(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=workspace["member_id"], collections=["col-a"],
        )

    _run(db, grant)

    async def disable(uow) -> None:
        await disable_user(
            uow, EventWriter(), context=context,
            target_user_id=workspace["member_id"],
        )

    _run(db, disable)

    async def access(uow) -> object:
        return await accessible_collection_ids(
            uow, user_id=workspace["member_id"], workspace_id=workspace["workspace_id"]
        )

    assert _run(db, access) == []


def test_member_gets_no_user_information_over_api(db, workspace) -> None:
    with TestClient(api.app) as client:
        sign_in = client.post(
            "/api/session", json={"email": MEMBER_EMAIL, "password": "member-password-1"}
        )
        listed = client.get("/api/users")
        invitations = client.get("/api/users/invitations")
        mutate = client.put(
            f"/api/users/{workspace['owner_id']}/collections",
            json={"collections": ["col-escalated"]},
        )

    assert sign_in.status_code == 200
    assert listed.status_code == 403
    assert invitations.status_code == 403
    assert mutate.status_code == 403


def test_unknown_and_foreign_target_404_identical_over_api(db, workspace) -> None:
    foreign_workspace = str(uuid.uuid4())
    foreign_user = str(uuid.uuid4())

    async def seed() -> None:
        from identity_access.passwords import hash_password

        async def run() -> None:
            engine = create_async_engine(Settings.load().database_url)
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign 2')"),
                    {"id": foreign_workspace},
                )
                await connection.execute(
                    text(
                        "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                        "VALUES (:id, :workspace_id, 'foreign2@example.com', :hash, 'owner')"
                    ),
                    {"id": foreign_user, "workspace_id": foreign_workspace, "hash": hash_password("foreign-pass-1")},
                )
            await engine.dispose()

        asyncio.run(run())

    seed()

    with TestClient(api.app) as client:
        assert client.post("/api/session", json={"email": OWNER_EMAIL, "password": "owner-password-1"}).status_code == 200
        missing = client.post(
            f"/api/users/{uuid.uuid4()}/password-reset", json={"new_password": "whatever-pass-1"}
        )
        foreign = client.post(
            f"/api/users/{foreign_user}/password-reset", json={"new_password": "whatever-pass-1"}
        )

    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.json() == foreign.json()


def test_target_not_found_never_leaks_via_service(db, workspace) -> None:
    context = AccessContext(
        user_id=workspace["owner_id"],
        workspace_id=workspace["workspace_id"],
        role="owner",
    )

    async def probe(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=str(uuid.uuid4()), collections=["col-a"],
        )

    with pytest.raises(TargetNotFound):
        _run(db, probe)
