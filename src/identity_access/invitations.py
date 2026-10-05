"""Invitation tokens and temporary passwords (Task 6d).

Invitations are single-use: accepting atomically creates the user, marks the
invitation accepted, and writes the audit events in one transaction. Reuse,
expiry, and revocation all fail with the same generic error. Temporary
passwords set ``must_change_password``; ``change_password`` verifies the old
password and revokes existing sessions so new credentials take everywhere.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.auth import token_hash
from identity_access.passwords import hash_password, verify_password

# ponytail: constants pending an owner decision; move to config/default.yaml when pinned
INVITATION_TTL_HOURS = 168


class InvitationInvalid(Exception):
    """Generic invitation failure: unknown, expired, reused, or revoked."""


@dataclass(frozen=True)
class IssuedInvitation:
    invitation_id: str
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class AcceptedInvitation:
    user_id: str
    workspace_id: str
    email: str
    role: str
    must_change_password: bool


def require_password_change(user: object) -> bool:
    """True when the session must be limited to password change."""
    return bool(getattr(user, "must_change_password", False))


async def issue_invitation_token(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    workspace_id: str,
    email: str,
    role: str,
    invited_by: str,
    ttl: timedelta = timedelta(hours=INVITATION_TTL_HOURS),
    now: datetime | None = None,
) -> IssuedInvitation:
    at = now or datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    invitation_id = str(uuid.uuid4())
    expires_at = at + ttl
    async with uow.transaction() as transaction:
        await transaction.execute(
            text(
                "INSERT INTO invitations (id, workspace_id, email, role, token_hash, "
                "status, invited_by, expires_at) "
                "VALUES (:id, :workspace_id, :email, :role, :token_hash, 'pending', "
                ":invited_by, :expires_at)"
            ),
            {
                "id": invitation_id,
                "workspace_id": workspace_id,
                "email": email,
                "role": role,
                "token_hash": token_hash(token),
                "invited_by": invited_by,
                "expires_at": expires_at,
            },
        )
        await writer.record(
            AuditEvent(type="invitation.issued", actor=invited_by, subject=invitation_id),
            transaction,
        )
    return IssuedInvitation(
        invitation_id=invitation_id, token=token, expires_at=expires_at
    )


async def accept_invitation(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    token: str,
    password: str,
    now: datetime | None = None,
) -> AcceptedInvitation:
    at = now or datetime.now(UTC)
    async with uow.transaction() as transaction:
        outcome = await _accept_in_transaction(transaction, writer, token, password, at)
    if isinstance(outcome, InvitationInvalid):
        raise outcome
    return outcome


async def _accept_in_transaction(
    transaction: AsyncConnection,
    writer: EventWriter,
    token: str,
    password: str,
    now: datetime,
) -> AcceptedInvitation | InvitationInvalid:
    invitation = (
        (
            await transaction.execute(
                text(
                    "SELECT id, workspace_id, email, role, status, expires_at "
                    "FROM invitations WHERE token_hash = :token_hash"
                ),
                {"token_hash": token_hash(token)},
            )
        )
        .mappings()
        .first()
    )
    if (
        invitation is None
        or invitation["status"] != "pending"
        or invitation["expires_at"] <= now
    ):
        return InvitationInvalid()
    duplicate = await transaction.execute(
        text(
            "SELECT 1 FROM users WHERE workspace_id = :workspace_id AND email = :email"
        ),
        {"workspace_id": invitation["workspace_id"], "email": invitation["email"]},
    )
    if duplicate.first() is not None:
        return InvitationInvalid()
    user_id = str(uuid.uuid4())
    await transaction.execute(
        text(
            "INSERT INTO users (id, workspace_id, email, password_hash, role, status) "
            "VALUES (:id, :workspace_id, :email, :password_hash, :role, 'active')"
        ),
        {
            "id": user_id,
            "workspace_id": invitation["workspace_id"],
            "email": invitation["email"],
            "password_hash": hash_password(password),
            "role": invitation["role"],
        },
    )
    await transaction.execute(
        text(
            "UPDATE invitations SET status = 'accepted', accepted_at = :accepted_at, "
            "updated_at = now() WHERE id = :id"
        ),
        {"accepted_at": now, "id": invitation["id"]},
    )
    await writer.record(
        AuditEvent(
            type="invitation.accepted",
            actor=invitation["email"],
            subject=str(invitation["id"]),
        ),
        transaction,
    )
    return AcceptedInvitation(
        user_id=user_id,
        workspace_id=str(invitation["workspace_id"]),
        email=str(invitation["email"]),
        role=str(invitation["role"]),
        must_change_password=False,
    )


async def revoke_invitation(
    uow: UnitOfWork, writer: EventWriter, invitation_id: str, *, actor: str
) -> bool:
    async with uow.transaction() as transaction:
        result = await transaction.execute(
            text(
                "UPDATE invitations SET status = 'revoked', updated_at = now() "
                "WHERE id = :id AND status = 'pending'"
            ),
            {"id": invitation_id},
        )
        if result.rowcount == 0:
            return False
        await writer.record(
            AuditEvent(type="invitation.revoked", actor=actor, subject=invitation_id),
            transaction,
        )
        return True


async def set_temporary_password(
    uow: UnitOfWork, writer: EventWriter, *, user_id: str, temp_password: str
) -> None:
    """Issue a temporary password; the holder must change it at next sign-in."""
    async with uow.transaction() as transaction:
        await transaction.execute(
            text(
                "UPDATE users SET password_hash = :password_hash, "
                "must_change_password = true, failed_attempts = 0, locked_until = NULL, "
                "updated_at = now() WHERE id = :id"
            ),
            {"password_hash": hash_password(temp_password), "id": user_id},
        )
        await writer.record(
            AuditEvent(
                type="user.temporary_password_set", actor="admin", subject=user_id
            ),
            transaction,
        )


async def reset_user_password(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    user_id: str,
    new_password: str,
    actor: str,
) -> None:
    """Set a new password, force a change at next sign-in, revoke all sessions.

    Single transaction so recovery can never leave a half-reset account with
    live sessions. Host recovery (Task 6e) and admin resets (Task 7) share it.
    """
    async with uow.transaction() as transaction:
        result = await transaction.execute(
            text(
                "UPDATE users SET password_hash = :password_hash, "
                "must_change_password = true, failed_attempts = 0, locked_until = NULL, "
                "updated_at = now() WHERE id = :id"
            ),
            {"password_hash": hash_password(new_password), "id": user_id},
        )
        if result.rowcount == 0:
            raise ValueError(f"no such user: {user_id}")
        await transaction.execute(
            text(
                "UPDATE sessions SET revoked_at = now() "
                "WHERE user_id = :id AND revoked_at IS NULL"
            ),
            {"id": user_id},
        )
        await writer.record(
            AuditEvent(type="user.password_reset", actor=actor, subject=user_id),
            transaction,
        )


async def change_password(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    user_id: str,
    current_password: str,
    new_password: str,
) -> bool:
    """Verify the current password, set the new one, revoke live sessions."""
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text("SELECT password_hash FROM users WHERE id = :id"),
                    {"id": user_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None or not verify_password(current_password, row["password_hash"]):
            return False
        await transaction.execute(
            text(
                "UPDATE users SET password_hash = :password_hash, "
                "must_change_password = false, updated_at = now() WHERE id = :id"
            ),
            {"password_hash": hash_password(new_password), "id": user_id},
        )
        await transaction.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE user_id = :id AND revoked_at IS NULL"),
            {"id": user_id},
        )
        await writer.record(
            AuditEvent(type="user.password_changed", actor=str(user_id), subject=user_id),
            transaction,
        )
        return True
