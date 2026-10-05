"""Role-aware user administration on Task 6 primitives (Task 7).

Every function authorizes first via ``authorize`` (Task 7b rules), then
delegates the primitive. Targets that do not exist or live in another
workspace raise ``TargetNotFound`` — routes return one generic 404 for
both, so user IDs cannot be probed across workspaces.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access import invitations
from identity_access.authorization import (
    AccessContext,
    ProtectedResource,
    authorize,
)
from identity_access.invitations import IssuedInvitation

INVITABLE_ROLES = ("admin", "member")


class TargetNotFound(Exception):
    """The user or invitation does not exist, or belongs to another workspace."""


async def _load_user(transaction, user_id: str) -> dict | None:
    row = (
        (
            await transaction.execute(
                text(
                    "SELECT id, workspace_id, role, status FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _authorize_target(
    action: str, target: dict, context: AccessContext
) -> None:
    authorize(
        action,
        ProtectedResource(
            str(target["workspace_id"]), str(target["role"]), str(target["id"])
        ),
        context,
    )


async def _require_target(transaction, user_id: str, context: AccessContext) -> dict:
    target = await _load_user(transaction, user_id)
    if target is None or str(target["workspace_id"]) != context.workspace_id:
        raise TargetNotFound(user_id)
    return target


async def invite_user(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    email: str,
    role: str,
    collections: Sequence[str] = (),
) -> IssuedInvitation:
    if role not in INVITABLE_ROLES:
        raise ValueError(f"unsupported invite role: {role}")
    authorize(
        "user.invite", ProtectedResource(context.workspace_id, role), context
    )
    return await invitations.issue_invitation_token(
        uow,
        writer,
        workspace_id=context.workspace_id,
        email=email,
        role=role,
        invited_by=context.user_id,
        collections=collections,
    )


async def revoke_invitation(
    uow: UnitOfWork, writer: EventWriter, *, context: AccessContext, invitation_id: str
) -> bool:
    async with uow.transaction() as transaction:
        row = (
            await transaction.execute(
                text("SELECT workspace_id FROM invitations WHERE id = :id"),
                {"id": invitation_id},
            )
        ).scalar_one_or_none()
        if row is None or str(row) != context.workspace_id:
            raise TargetNotFound(invitation_id)
    authorize("invitation.revoke", ProtectedResource(context.workspace_id), context)
    return await invitations.revoke_invitation(
        uow, writer, invitation_id, actor=context.user_id
    )


async def set_user_collections(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    target_user_id: str,
    collections: Sequence[str],
) -> None:
    normalized = sorted(set(collections))
    async with uow.transaction() as transaction:
        target = await _require_target(transaction, target_user_id, context)
        _authorize_target("user.set_grants", target, context)
        await transaction.execute(
            text("DELETE FROM collection_grants WHERE user_id = :id"),
            {"id": target_user_id},
        )
        for collection_id in normalized:
            await transaction.execute(
                text(
                    "INSERT INTO collection_grants (id, workspace_id, user_id, collection_id) "
                    "VALUES (:id, :workspace_id, :user_id, :collection_id)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "workspace_id": target["workspace_id"],
                    "user_id": target_user_id,
                    "collection_id": collection_id,
                },
            )
        await writer.record(
            AuditEvent(
                type="user.grants_changed",
                actor=context.user_id,
                subject=target_user_id,
                metadata={"collections": normalized},
            ),
            transaction,
        )


async def reset_user_password(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    target_user_id: str,
    new_password: str,
) -> None:
    async with uow.transaction() as transaction:
        target = await _require_target(transaction, target_user_id, context)
    _authorize_target("user.reset_password", target, context)
    await invitations.reset_user_password(
        uow, writer, user_id=target_user_id, new_password=new_password,
        actor=context.user_id,
    )


async def disable_user(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    target_user_id: str,
) -> None:
    async with uow.transaction() as transaction:
        target = await _require_target(transaction, target_user_id, context)
        _authorize_target("user.disable", target, context)
        await transaction.execute(
            text(
                "UPDATE users SET status = 'disabled', updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": target_user_id},
        )
        await transaction.execute(
            text(
                "UPDATE sessions SET revoked_at = now() "
                "WHERE user_id = :id AND revoked_at IS NULL"
            ),
            {"id": target_user_id},
        )
        await writer.record(
            AuditEvent(
                type="user.disabled", actor=context.user_id, subject=target_user_id
            ),
            transaction,
        )
