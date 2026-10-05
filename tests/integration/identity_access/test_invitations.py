"""Task 6d: invitation-token and temporary-password primitives."""

import asyncio
from datetime import datetime, timedelta

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
    revoke_user_sessions,
)
from identity_access.invitations import (
    InvitationInvalid,
    accept_invitation,
    change_password,
    issue_invitation_token,
    require_password_change,
    revoke_invitation,
    set_temporary_password,
)
from identity_access.setup import create_first_owner

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "owner-password-1"
MEMBER_EMAIL = "member@example.com"


@pytest.fixture()
def workspace(db: Settings) -> dict:
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), email=OWNER_EMAIL, password=OWNER_PASSWORD)
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id, "owner_id": owner.user_id}

    return asyncio.run(create())


def _issue(db: Settings, workspace: dict, **kwargs) -> object:
    parameters = {
        "workspace_id": workspace["workspace_id"],
        "email": MEMBER_EMAIL,
        "role": "member",
        "invited_by": workspace["owner_id"],
    } | kwargs

    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await issue_invitation_token(uow, EventWriter(), **parameters)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _accept(db: Settings, token: str, password: str, now: datetime | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await accept_invitation(uow, EventWriter(), token=token, password=password, now=now)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_issue_then_accept_creates_member_and_marks_accepted(db, workspace) -> None:
    invitation = _issue(db, workspace)
    accepted = _accept(db, invitation.token, "member-password-1")

    assert accepted.user_id
    assert accepted.workspace_id == workspace["workspace_id"]
    assert accepted.role == "member"
    assert accepted.must_change_password is False

    async def rows() -> tuple[dict, int]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.connect() as connection:
                invitation_row = (
                    (
                        await connection.execute(
                            text("SELECT status, accepted_at FROM invitations")
                        )
                    )
                    .mappings()
                    .one()
                )
                audits = (
                    await connection.execute(
                        text("SELECT count(*) FROM audit_events WHERE type LIKE 'invitation.%'")
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        return dict(invitation_row), audits

    invitation_row, audits = asyncio.run(rows())
    assert invitation_row["status"] == "accepted"
    assert invitation_row["accepted_at"] is not None
    assert audits == 2  # issued + accepted


def test_expired_invitation_refused(db, workspace) -> None:
    invitation = _issue(db, workspace, ttl=timedelta(hours=-1))
    with pytest.raises(InvitationInvalid):
        _accept(db, invitation.token, "member-password-1")


def test_reused_invitation_refused(db, workspace) -> None:
    invitation = _issue(db, workspace)
    _accept(db, invitation.token, "member-password-1")
    with pytest.raises(InvitationInvalid):
        _accept(db, invitation.token, "member-password-1")


def test_revoked_invitation_refused(db, workspace) -> None:
    invitation = _issue(db, workspace)

    async def revoke() -> bool:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await revoke_invitation(uow, EventWriter(), invitation.invitation_id, actor=OWNER_EMAIL)
        finally:
            await engine.dispose()

    assert asyncio.run(revoke()) is True
    with pytest.raises(InvitationInvalid):
        _accept(db, invitation.token, "member-password-1")


def test_unknown_token_refused(db) -> None:
    with pytest.raises(InvitationInvalid):
        _accept(db, "not-a-token", "member-password-1")


def test_duplicate_email_refused(db, workspace) -> None:
    invitation = _issue(db, workspace, email=OWNER_EMAIL)
    with pytest.raises(InvitationInvalid):
        _accept(db, invitation.token, "member-password-1")


def test_temporary_password_forces_change(db, workspace) -> None:
    invitation = _issue(db, workspace)
    accepted = _accept(db, invitation.token, "member-password-1")

    async def set_temp() -> None:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            await set_temporary_password(uow, EventWriter(), user_id=accepted.user_id, temp_password="temp-pass-9")
        finally:
            await engine.dispose()

    asyncio.run(set_temp())

    async def signed_in_user() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            session = await authenticate(uow, EventWriter(), MEMBER_EMAIL, "temp-pass-9")
            return await require_session(uow, session.token)
        finally:
            await engine.dispose()

    user = asyncio.run(signed_in_user())
    assert require_password_change(user) is True

    async def change() -> None:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            await change_password(uow, EventWriter(), user_id=user.user_id, current_password="temp-pass-9", new_password="new-pass-10")
        finally:
            await engine.dispose()

    asyncio.run(change())

    async def verify() -> tuple[object, object]:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            session = await authenticate(uow, EventWriter(), MEMBER_EMAIL, "new-pass-10")
            resolved = await require_session(uow, session.token)
            old_failed = False
            try:
                await authenticate(uow, EventWriter(), MEMBER_EMAIL, "temp-pass-9")
            except AuthenticationFailed:
                old_failed = True
            return resolved, old_failed
        finally:
            await engine.dispose()

    resolved, old_failed = asyncio.run(verify())
    assert require_password_change(resolved) is False
    assert old_failed is True


def test_change_password_revokes_other_sessions(db, workspace) -> None:
    invitation = _issue(db, workspace)
    accepted = _accept(db, invitation.token, "member-password-1")

    async def setup_sessions() -> tuple[str, str]:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            first = await authenticate(uow, EventWriter(), MEMBER_EMAIL, "member-password-1")
            second = await authenticate(uow, EventWriter(), MEMBER_EMAIL, "member-password-1")
            await change_password(uow, EventWriter(), user_id=accepted.user_id, current_password="member-password-1", new_password="rotated-pass-11")
            return first.token, second.token
        finally:
            await engine.dispose()

    first_token, second_token = asyncio.run(setup_sessions())

    async def check(token: str) -> None:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            with pytest.raises(SessionInvalid):
                await require_session(uow, token)
        finally:
            await engine.dispose()

    asyncio.run(check(first_token))
    asyncio.run(check(second_token))


def test_revoke_unknown_invitation_returns_false(db, workspace) -> None:
    async def run() -> bool:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await revoke_invitation(uow, EventWriter(), "00000000-0000-0000-0000-000000000000", actor=OWNER_EMAIL)
        finally:
            await engine.dispose()

    assert asyncio.run(run()) is False


def test_change_password_wrong_current_returns_false(db, workspace) -> None:
    invitation = _issue(db, workspace)
    accepted = _accept(db, invitation.token, "member-password-1")

    async def run() -> bool:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await change_password(uow, EventWriter(), user_id=accepted.user_id, current_password="wrong", new_password="irrelevant-1")
        finally:
            await engine.dispose()

    assert asyncio.run(run()) is False


def test_revoke_user_sessions_helper_counts(db, workspace) -> None:
    invitation = _issue(db, workspace)
    accepted = _accept(db, invitation.token, "member-password-1")

    async def sign_in_twice_and_revoke() -> int:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            await authenticate(uow, EventWriter(), MEMBER_EMAIL, "member-password-1")
            await authenticate(uow, EventWriter(), MEMBER_EMAIL, "member-password-1")
            return await revoke_user_sessions(uow, accepted.user_id)
        finally:
            await engine.dispose()

    assert asyncio.run(sign_in_twice_and_revoke()) == 2
