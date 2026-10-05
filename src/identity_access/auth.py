"""Authentication, sign-in throttling, and session lifecycle (Task 6b).

``authenticate`` verifies an Argon2id password and issues a session whose raw
token is shown once — only its SHA-256 hash is stored. Repeated failures lock
the account for a window. The failure-counter update must survive the failed
attempt, so the ``AuthenticationFailed`` exception is raised after the
transaction commits, never inside it.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.passwords import verify_password

# ponytail: constants pending an owner decision; move to config/default.yaml when pinned
SESSION_TTL_HOURS = 12
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class AuthenticationFailed(Exception):
    """Generic sign-in failure; never reveals which part was wrong."""


class SessionInvalid(Exception):
    """Unknown, expired, or revoked session."""


@dataclass(frozen=True)
class IssuedSession:
    user_id: str
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    workspace_id: str
    email: str
    role: str
    must_change_password: bool


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def authenticate(
    uow: UnitOfWork,
    writer: EventWriter,
    email: str,
    password: str,
    *,
    now: datetime | None = None,
) -> IssuedSession:
    at = now or _now()
    async with uow.transaction() as transaction:
        outcome = await _attempt_sign_in(transaction, writer, email, password, at)
    if isinstance(outcome, AuthenticationFailed):
        raise outcome
    return outcome


async def _attempt_sign_in(
    transaction: AsyncConnection,
    writer: EventWriter,
    email: str,
    password: str,
    now: datetime,
) -> IssuedSession | AuthenticationFailed:
    row = (
        (
            await transaction.execute(
                text(
                    "SELECT id, password_hash, status, failed_attempts, locked_until "
                    "FROM users WHERE email = :email"
                ),
                {"email": email},
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["status"] != "active":
        return AuthenticationFailed()
    if row["locked_until"] is not None and row["locked_until"] > now:
        return AuthenticationFailed()
    if not verify_password(password, row["password_hash"]):
        attempts = row["failed_attempts"] + 1
        if attempts >= MAX_FAILED_ATTEMPTS:
            await transaction.execute(
                text(
                    "UPDATE users SET failed_attempts = :attempts, locked_until = :locked, "
                    "updated_at = now() WHERE id = :id"
                ),
                {
                    "attempts": attempts,
                    "locked": now + timedelta(minutes=LOCKOUT_MINUTES),
                    "id": row["id"],
                },
            )
            await writer.record(
                AuditEvent(
                    type="auth.locked_out", actor=email, subject=str(row["id"])
                ),
                transaction,
            )
        else:
            await transaction.execute(
                text(
                    "UPDATE users SET failed_attempts = :attempts, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"attempts": attempts, "id": row["id"]},
            )
        return AuthenticationFailed()
    await transaction.execute(
        text(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": row["id"]},
    )
    token = secrets.token_urlsafe(32)
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    await transaction.execute(
        text(
            "INSERT INTO sessions (id, user_id, token_hash, expires_at) "
            "VALUES (:id, :user_id, :token_hash, :expires_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "user_id": row["id"],
            "token_hash": token_hash(token),
            "expires_at": expires_at,
        },
    )
    await writer.record(
        AuditEvent(type="auth.sign_in", actor=email, subject=str(row["id"])),
        transaction,
    )
    return IssuedSession(user_id=str(row["id"]), token=token, expires_at=expires_at)


async def require_session(
    uow: UnitOfWork, token: str, *, now: datetime | None = None
) -> AuthenticatedUser:
    at = now or _now()
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text(
                        "SELECT u.id AS user_id, u.workspace_id, u.email, u.role, "
                        "u.status, u.must_change_password, s.expires_at, s.revoked_at "
                        "FROM sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash = :token_hash"
                    ),
                    {"token_hash": token_hash(token)},
                )
            )
            .mappings()
            .first()
        )
    if (
        row is None
        or row["revoked_at"] is not None
        or row["expires_at"] <= at
        or row["status"] != "active"
    ):
        raise SessionInvalid()
    return AuthenticatedUser(
        user_id=str(row["user_id"]),
        workspace_id=str(row["workspace_id"]),
        email=str(row["email"]),
        role=str(row["role"]),
        must_change_password=bool(row["must_change_password"]),
    )


async def revoke_session(uow: UnitOfWork, token: str) -> bool:
    async with uow.transaction() as transaction:
        result = await transaction.execute(
            text(
                "UPDATE sessions SET revoked_at = now() "
                "WHERE token_hash = :token_hash AND revoked_at IS NULL"
            ),
            {"token_hash": token_hash(token)},
        )
        return result.rowcount > 0


async def revoke_user_sessions(uow: UnitOfWork, user_id: str) -> int:
    """Revoke every live session for a user (lockout recovery, disable-user)."""
    async with uow.transaction() as transaction:
        result = await transaction.execute(
            text(
                "UPDATE sessions SET revoked_at = now() "
                "WHERE user_id = :user_id AND revoked_at IS NULL"
            ),
            {"user_id": user_id},
        )
        return result.rowcount
