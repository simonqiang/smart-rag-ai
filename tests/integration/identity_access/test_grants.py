"""Tasks 7a/7b: collection grants, user administration primitives, role rules."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.auth import (
    AuthenticationFailed,
    SessionInvalid,
    authenticate,
    require_session,
)
from identity_access.authorization import AccessContext, AccessDenied
from identity_access.grants import (
    TargetNotFound,
    disable_user,
    invite_user,
    reset_user_password,
    set_user_collections,
)
from identity_access.invitations import accept_invitation, issue_invitation_token
from identity_access.passwords import hash_password
from identity_access.setup import create_first_owner

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "owner-password-1"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password-1"
MEMBER_EMAIL = "member@example.com"
MEMBER_PASSWORD = "member-password-1"


def _run(settings: Settings, coro_factory) -> object:
    async def run() -> object:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        try:
            return await coro_factory(uow)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture()
def workspace(db: Settings) -> dict:
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), email=OWNER_EMAIL, password=OWNER_PASSWORD)
            admin_invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id, email=ADMIN_EMAIL,
                role="admin", invited_by=owner.user_id,
            )
            member_invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id, email=MEMBER_EMAIL,
                role="member", invited_by=owner.user_id,
            )
            admin = await accept_invitation(uow, EventWriter(), token=admin_invite.token, password=ADMIN_PASSWORD)
            member = await accept_invitation(uow, EventWriter(), token=member_invite.token, password=MEMBER_PASSWORD)
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "admin_id": admin.user_id,
            "member_id": member.user_id,
        }

    return asyncio.run(create())


def _context(workspace: dict, key: str, role: str) -> AccessContext:
    return AccessContext(
        user_id=workspace[key], workspace_id=workspace["workspace_id"], role=role
    )


def test_accept_invitation_with_collections_inserts_grants(db, workspace) -> None:
    async def run(uow) -> object:
        invitation = await issue_invitation_token(
            uow, EventWriter(), workspace_id=workspace["workspace_id"],
            email="scoped@example.com", role="member",
            invited_by=workspace["owner_id"], collections=["col-a", "col-b"],
        )
        return invitation, await accept_invitation(
            uow, EventWriter(), token=invitation.token, password="scoped-pass-1"
        )

    _, accepted = _run(db, run)
    assert sorted(accepted.collections) == ["col-a", "col-b"]

    async def grants(uow) -> list:
        async with uow.transaction() as transaction:
            rows = (
                await transaction.execute(
                    text("SELECT collection_id FROM collection_grants WHERE user_id = :id ORDER BY 1"),
                    {"id": accepted.user_id},
                )
            ).scalars().all()
        return list(rows)

    assert _run(db, grants) == ["col-a", "col-b"]


def test_accept_invitation_without_collections_grants_nothing(db, workspace) -> None:
    async def count(uow) -> int:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(text("SELECT count(*) FROM collection_grants"))
            ).scalar_one()

    assert _run(db, count) == 0


def test_set_user_collections_replaces_atomically(db, workspace) -> None:
    context = _context(workspace, "owner_id", "owner")

    async def run(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=workspace["member_id"], collections=["col-c", "col-a", "col-a"],
        )
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=workspace["member_id"], collections=["col-b", "col-c"],
        )

    _run(db, run)

    async def verify(uow) -> tuple[list, int]:
        async with uow.transaction() as transaction:
            rows = (
                await transaction.execute(
                    text("SELECT collection_id FROM collection_grants WHERE user_id = :id ORDER BY 1"),
                    {"id": workspace["member_id"]},
                )
            ).scalars().all()
            audits = (
                await transaction.execute(
                    text("SELECT count(*) FROM audit_events WHERE type = 'user.grants_changed'")
                )
            ).scalar_one()
        return list(rows), audits

    rows, audits = _run(db, verify)
    assert rows == ["col-b", "col-c"]
    assert audits == 2


def test_set_user_collections_unknown_target_raises_target_not_found(db, workspace) -> None:
    context = _context(workspace, "owner_id", "owner")

    async def run(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=str(uuid.uuid4()), collections=["col-a"],
        )

    with pytest.raises(TargetNotFound):
        _run(db, run)


def test_set_user_collections_cross_workspace_target_is_not_found(db, workspace) -> None:
    foreign_workspace = str(uuid.uuid4())
    foreign_user = str(uuid.uuid4())

    async def seed(uow) -> None:
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": foreign_workspace},
            )
            await transaction.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                    "VALUES (:id, :workspace_id, 'foreign@example.com', :hash, 'owner')"
                ),
                {"id": foreign_user, "workspace_id": foreign_workspace, "hash": hash_password("foreign-pass-1")},
            )

    _run(db, seed)
    context = _context(workspace, "owner_id", "owner")

    async def run(uow) -> None:
        await set_user_collections(
            uow, EventWriter(), context=context,
            target_user_id=foreign_user, collections=["col-a"],
        )

    with pytest.raises(TargetNotFound):
        _run(db, run)


def test_accessible_collection_ids_by_role(db, workspace) -> None:
    async def seed(uow) -> None:
        await set_user_collections(
            uow, EventWriter(),
            context=_context(workspace, "owner_id", "owner"),
            target_user_id=workspace["member_id"], collections=["col-a", "col-b"],
        )

    _run(db, seed)

    async def access(uow, *, user_id: str) -> object:
        from identity_access.authorization import accessible_collection_ids

        return await accessible_collection_ids(
            uow, user_id=user_id, workspace_id=workspace["workspace_id"]
        )

    assert _run(db, lambda uow: access(uow, user_id=workspace["owner_id"])) is None
    assert _run(db, lambda uow: access(uow, user_id=workspace["admin_id"])) is None
    assert _run(db, lambda uow: access(uow, user_id=workspace["member_id"])) == ["col-a", "col-b"]


def test_member_without_grants_has_empty_access(db, workspace) -> None:
    async def access(uow) -> object:
        from identity_access.authorization import accessible_collection_ids

        return await accessible_collection_ids(
            uow, user_id=workspace["member_id"], workspace_id=workspace["workspace_id"]
        )

    assert _run(db, access) == []


def test_accessible_collection_ids_unknown_user_has_no_access(db, workspace) -> None:
    async def access(uow) -> object:
        from identity_access.authorization import accessible_collection_ids

        return await accessible_collection_ids(
            uow, user_id=str(uuid.uuid4()), workspace_id=workspace["workspace_id"]
        )

    assert _run(db, access) == []


def test_disable_user_revokes_sessions_and_blocks_sign_in(db, workspace) -> None:
    session = _run(
        db,
        lambda uow: authenticate(uow, EventWriter(), MEMBER_EMAIL, MEMBER_PASSWORD),
    )

    async def disable(uow) -> None:
        await disable_user(
            uow, EventWriter(),
            context=_context(workspace, "admin_id", "admin"),
            target_user_id=workspace["member_id"],
        )

    _run(db, disable)

    async def verify_full(uow) -> tuple[bool, bool, int]:
        session_gone = sign_in_gone = False
        try:
            await require_session(uow, session.token)
        except SessionInvalid:
            session_gone = True
        try:
            await authenticate(uow, EventWriter(), MEMBER_EMAIL, MEMBER_PASSWORD)
        except AuthenticationFailed:
            sign_in_gone = True
        async with uow.transaction() as transaction:
            audits = (
                await transaction.execute(
                    text("SELECT count(*) FROM audit_events WHERE type = 'user.disabled'")
                )
            ).scalar_one()
        return session_gone, sign_in_gone, audits

    session_gone, sign_in_gone, audits = _run(db, verify_full)
    assert (session_gone, sign_in_gone, audits) == (True, True, 1)


def test_admin_cannot_disable_admin_or_owner_or_self(db, workspace) -> None:
    async def run(uow, target: str) -> None:
        await disable_user(
            uow, EventWriter(),
            context=_context(workspace, "admin_id", "admin"),
            target_user_id=workspace[target],
        )

    with pytest.raises(AccessDenied):
        _run(db, lambda uow: run(uow, "owner_id"))
    with pytest.raises(AccessDenied):
        _run(db, lambda uow: run(uow, "admin_id"))


def test_owner_can_disable_admin(db, workspace) -> None:
    async def run(uow) -> None:
        await disable_user(
            uow, EventWriter(),
            context=_context(workspace, "owner_id", "owner"),
            target_user_id=workspace["admin_id"],
        )

    _run(db, run)

    async def status(uow) -> str:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(
                    text("SELECT status FROM users WHERE id = :id"),
                    {"id": workspace["admin_id"]},
                )
            ).scalar_one()

    assert _run(db, status) == "disabled"


def test_invite_role_rules(db, workspace) -> None:
    async def invite(uow, *, context, role: str) -> None:
        await invite_user(
            uow, EventWriter(), context=context,
            email="newcomer@example.com", role=role, collections=[],
        )

    owner = _context(workspace, "owner_id", "owner")
    admin = _context(workspace, "admin_id", "admin")
    member = _context(workspace, "member_id", "member")

    _run(db, lambda uow: invite(uow, context=owner, role="admin"))
    _run(db, lambda uow: invite(uow, context=owner, role="member"))
    _run(db, lambda uow: invite(uow, context=admin, role="member"))
    with pytest.raises(AccessDenied):
        _run(db, lambda uow: invite(uow, context=admin, role="admin"))
    with pytest.raises(AccessDenied):
        _run(db, lambda uow: invite(uow, context=member, role="member"))


def test_invite_rejects_owner_role_and_sets_invited_by(db, workspace) -> None:
    async def invite(uow) -> object:
        return await invite_user(
            uow, EventWriter(),
            context=_context(workspace, "owner_id", "owner"),
            email="second-owner@example.com", role="owner", collections=[],
        )

    with pytest.raises(ValueError):
        _run(db, invite)

    async def invited(uow) -> object:
        issued = await invite_user(
            uow, EventWriter(),
            context=_context(workspace, "admin_id", "admin"),
            email="invited@example.com", role="member", collections=["col-a"],
        )
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT invited_by FROM invitations WHERE id = :id"),
                    {"id": issued.invitation_id},
                )
            ).scalar_one()
        return row

    assert _run(db, invited) == uuid.UUID(workspace["admin_id"])


def test_reset_password_role_rules(db, workspace) -> None:
    async def reset(uow, *, context, target: str) -> None:
        await reset_user_password(
            uow, EventWriter(), context=context,
            target_user_id=workspace[target], new_password="reset-pass-1",
        )

    admin = _context(workspace, "admin_id", "admin")
    owner = _context(workspace, "owner_id", "owner")

    session = _run(
        db,
        lambda uow: authenticate(uow, EventWriter(), MEMBER_EMAIL, MEMBER_PASSWORD),
    )
    _run(db, lambda uow: reset(uow, context=admin, target="member_id"))

    async def verify(uow) -> tuple[bool, bool]:
        forced = False
        old_gone = False
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT must_change_password FROM users WHERE id = :id"),
                    {"id": workspace["member_id"]},
                )
            ).scalar_one()
        forced = bool(row)
        try:
            await require_session(uow, session.token)
        except SessionInvalid:
            old_gone = True
        return forced, old_gone

    forced, old_gone = _run(db, verify)
    assert (forced, old_gone) == (True, True)

    _run(db, lambda uow: reset(uow, context=owner, target="admin_id"))
    with pytest.raises(AccessDenied):
        _run(db, lambda uow: reset(uow, context=admin, target="admin_id"))
    with pytest.raises(AccessDenied):
        _run(db, lambda uow: reset(uow, context=admin, target="owner_id"))


def test_reset_password_unknown_target_raises_target_not_found(db, workspace) -> None:
    async def run(uow) -> None:
        await reset_user_password(
            uow, EventWriter(),
            context=_context(workspace, "owner_id", "owner"),
            target_user_id=str(uuid.uuid4()), new_password="reset-pass-1",
        )

    with pytest.raises(TargetNotFound):
        _run(db, run)


def test_revoke_invitation_unknown_or_foreign_raises_target_not_found(db, workspace) -> None:
    from identity_access.grants import revoke_invitation as revoke

    async def seed_foreign(uow) -> str:
        invitation_id = str(uuid.uuid4())
        workspace_id = str(uuid.uuid4())
        async with uow.transaction() as transaction:
            await transaction.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, 'Foreign')"),
                {"id": workspace_id},
            )
            await transaction.execute(
                text(
                    "INSERT INTO invitations (id, workspace_id, email, role, token_hash, "
                    "status, expires_at) VALUES (:id, :workspace_id, 'f@example.com', "
                    "'member', 'hash', 'pending', now() + interval '1 day')"
                ),
                {"id": invitation_id, "workspace_id": workspace_id},
            )
        return invitation_id

    # The foreign invitation needs a real workspace row; insert one and use its id.
    async def run_unknown(uow) -> None:
        await revoke(
            uow, EventWriter(),
            context=_context(workspace, "owner_id", "owner"),
            invitation_id=str(uuid.uuid4()),
        )

    with pytest.raises(TargetNotFound):
        _run(db, run_unknown)

    async def seed_and_revoke(uow) -> None:
        invitation_id = await seed_foreign(uow)
        await revoke(
            uow, EventWriter(),
            context=_context(workspace, "owner_id", "owner"),
            invitation_id=invitation_id,
        )

    with pytest.raises(TargetNotFound):
        _run(db, seed_and_revoke)
