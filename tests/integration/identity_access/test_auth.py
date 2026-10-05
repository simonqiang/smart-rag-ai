"""Task 6b: authentication, sign-in throttling, and session lifecycle."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

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
    revoke_session,
    revoke_user_sessions,
)
from identity_access.setup import create_first_owner

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "owner-password-1"


@pytest.fixture()
def owner(db: Settings) -> None:
    async def create() -> None:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            await create_first_owner(uow, EventWriter(), email=OWNER_EMAIL, password=OWNER_PASSWORD)
        finally:
            await engine.dispose()

    asyncio.run(create())


def _user_row(db: Settings) -> dict:
    async def read() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT failed_attempts, locked_until FROM users "
                                "WHERE email = :email"
                            ),
                            {"email": OWNER_EMAIL},
                        )
                    )
                    .mappings()
                    .one()
                )
        finally:
            await engine.dispose()
        return dict(row)

    return asyncio.run(read())


def _sign_in(db: Settings, password: str, now: datetime | None = None):
    async def run():
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await authenticate(uow, EventWriter(), OWNER_EMAIL, password, now=now)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_correct_password_issues_session_with_hashed_token(db, owner) -> None:
    session = _sign_in(db, OWNER_PASSWORD)
    assert session.token
    assert session.expires_at > datetime.now(UTC)

    async def read() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text("SELECT token_hash FROM sessions WHERE user_id = :uid"),
                            {"uid": session.user_id},
                        )
                    )
                    .mappings()
                    .one()
                )
        finally:
            await engine.dispose()
        return row["token_hash"]

    assert asyncio.run(read()) == hashlib.sha256(session.token.encode()).hexdigest()


def test_wrong_password_increments_failure_counter(db, owner) -> None:
    with pytest.raises(AuthenticationFailed):
        _sign_in(db, "wrong-password")
    assert _user_row(db)["failed_attempts"] == 1


def test_repeated_failures_lock_and_refuse_correct_password(db, owner) -> None:
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            _sign_in(db, "wrong-password")
    row = _user_row(db)
    assert row["locked_until"] is not None
    with pytest.raises(AuthenticationFailed):
        _sign_in(db, OWNER_PASSWORD)  # correct password still refused while locked


def test_lockout_expiry_restores_sign_in_and_resets_counter(db, owner) -> None:
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            _sign_in(db, "wrong-password")
    session = _sign_in(db, OWNER_PASSWORD, now=datetime.now(UTC) + timedelta(minutes=20))
    assert session.token
    assert _user_row(db)["failed_attempts"] == 0


def test_unknown_email_fails_without_session(db) -> None:
    with pytest.raises(AuthenticationFailed):
        _sign_in(db, OWNER_PASSWORD)


def _require(db: Settings, token: str, now: datetime | None = None):
    async def run():
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await require_session(uow, token, now=now)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_valid_session_resolves_user(db, owner) -> None:
    session = _sign_in(db, OWNER_PASSWORD)
    user = _require(db, session.token)
    assert user.email == OWNER_EMAIL
    assert user.role == "owner"
    assert user.workspace_id


def test_expired_session_is_invalid(db, owner) -> None:
    session = _sign_in(db, OWNER_PASSWORD)
    with pytest.raises(SessionInvalid):
        _require(db, session.token, now=datetime.now(UTC) + timedelta(hours=13))


def test_revoked_session_is_invalid(db, owner) -> None:
    session = _sign_in(db, OWNER_PASSWORD)
    assert asyncio.run(_revoke(db, session.token)) is True
    with pytest.raises(SessionInvalid):
        _require(db, session.token)


def test_revoke_user_sessions_revokes_all_and_returns_count(db, owner) -> None:
    first = _sign_in(db, OWNER_PASSWORD)
    second = _sign_in(db, OWNER_PASSWORD)

    async def run() -> int:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await revoke_user_sessions(uow, first.user_id)
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == 2
    with pytest.raises(SessionInvalid):
        _require(db, first.token)
    with pytest.raises(SessionInvalid):
        _require(db, second.token)


def test_revoke_session_unknown_token_returns_false(db) -> None:
    assert asyncio.run(_revoke(db, "not-a-token")) is False


async def _revoke(db: Settings, token: str) -> bool:
    engine = create_async_engine(db.database_url)
    uow = UnitOfWork(engine)
    try:
        return await revoke_session(uow, token)
    finally:
        await engine.dispose()
