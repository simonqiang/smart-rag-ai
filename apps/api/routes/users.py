"""User administration routes (Task 7c).

Role rules are enforced twice: a cheap pre-check here so members always get
403 (never 404 for a target they cannot see), and the real target-aware
check inside ``grants``. Unknown and cross-workspace IDs collapse into one
generic 404 via the ``TargetNotFound`` exception handler in ``main``.
``_admin_context`` leaves ``collection_ids`` unset on purpose: these routes
are role-gated; catalog queries (Task 8) must populate grants themselves.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from apps.api.routes.auth import _check_origin, current_user, uow
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import (
    AccessContext,
    ProtectedResource,
    authorize,
)
from identity_access.grants import (
    TargetNotFound,
    disable_user,
    invite_user,
    reset_user_password,
    revoke_invitation,
    set_user_collections,
)

router = APIRouter(prefix="/api/users")


async def ready_user(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> AuthenticatedUser:
    """Sessions flagged for password change stay limited to that change."""
    if user.must_change_password:
        raise HTTPException(
            status_code=403, detail="password change required before using this account"
        )
    return user


def _admin_context(user: AuthenticatedUser) -> AccessContext:
    return AccessContext(
        user_id=user.user_id, workspace_id=user.workspace_id, role=user.role
    )


class InviteRequest(BaseModel):
    email: str
    role: Literal["admin", "member"]
    collections: list[str] = []


class CollectionsRequest(BaseModel):
    collections: list[str] = []


class PasswordResetRequest(BaseModel):
    new_password: str


@router.get("")
async def list_users(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    authorize("user.list", ProtectedResource(user.workspace_id), _admin_context(user))
    async with uow().transaction() as transaction:
        users = (
            await transaction.execute(
                text(
                    "SELECT id, email, role, status, must_change_password FROM users "
                    "WHERE workspace_id = :workspace_id ORDER BY created_at"
                ),
                {"workspace_id": user.workspace_id},
            )
        ).mappings().all()
        grants = (
            await transaction.execute(
                text(
                    "SELECT user_id, collection_id FROM collection_grants "
                    "WHERE workspace_id = :workspace_id ORDER BY collection_id"
                ),
                {"workspace_id": user.workspace_id},
            )
        ).mappings().all()
    by_user: dict[str, list[str]] = {}
    for row in grants:
        by_user.setdefault(str(row["user_id"]), []).append(str(row["collection_id"]))
    return [
        {
            "user_id": str(row["id"]),
            "email": str(row["email"]),
            "role": str(row["role"]),
            "status": str(row["status"]),
            "must_change_password": bool(row["must_change_password"]),
            "collections": by_user.get(str(row["id"]), []),
        }
        for row in users
    ]


@router.get("/invitations")
async def list_invitations(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    authorize("user.list", ProtectedResource(user.workspace_id), _admin_context(user))
    async with uow().transaction() as transaction:
        rows = (
            await transaction.execute(
                text(
                    "SELECT id, email, role, collections, expires_at FROM invitations "
                    "WHERE workspace_id = :workspace_id AND status = 'pending' "
                    "ORDER BY created_at"
                ),
                {"workspace_id": user.workspace_id},
            )
        ).mappings().all()
    return [
        {
            "invitation_id": str(row["id"]),
            "email": str(row["email"]),
            "role": str(row["role"]),
            "collections": json.loads(row["collections"] or "[]"),
            "expires_at": row["expires_at"].isoformat(),
        }
        for row in rows
    ]


@router.post("/invitations", status_code=201)
async def invite(
    request: Request,
    body: InviteRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = _admin_context(user)
    authorize("user.invite", ProtectedResource(context.workspace_id, body.role), context)
    issued = await invite_user(
        uow(),
        EventWriter(),
        context=context,
        email=body.email,
        role=body.role,
        collections=body.collections,
    )
    return {
        "invitation_id": issued.invitation_id,
        "token": issued.token,
        "expires_at": issued.expires_at.isoformat(),
    }


@router.delete("/invitations/{invitation_id}", status_code=204)
async def revoke(
    invitation_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> None:
    _check_origin(request)
    context = _admin_context(user)
    authorize("invitation.revoke", ProtectedResource(context.workspace_id), context)
    revoked = await revoke_invitation(
        uow(), EventWriter(), context=context, invitation_id=invitation_id
    )
    if not revoked:
        raise TargetNotFound(invitation_id)


@router.put("/{user_id}/collections", status_code=204)
async def put_collections(
    user_id: str,
    request: Request,
    body: CollectionsRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> None:
    _check_origin(request)
    context = _admin_context(user)
    authorize("user.set_grants", ProtectedResource(context.workspace_id), context)
    await set_user_collections(
        uow(),
        EventWriter(),
        context=context,
        target_user_id=user_id,
        collections=body.collections,
    )


@router.post("/{user_id}/password-reset", status_code=204)
async def reset_password(
    user_id: str,
    request: Request,
    body: PasswordResetRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> None:
    _check_origin(request)
    context = _admin_context(user)
    authorize("user.reset_password", ProtectedResource(context.workspace_id), context)
    await reset_user_password(
        uow(),
        EventWriter(),
        context=context,
        target_user_id=user_id,
        new_password=body.new_password,
    )


@router.post("/{user_id}/disable", status_code=204)
async def disable(
    user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> None:
    _check_origin(request)
    context = _admin_context(user)
    authorize("user.disable", ProtectedResource(context.workspace_id), context)
    await disable_user(uow(), EventWriter(), context=context, target_user_id=user_id)
