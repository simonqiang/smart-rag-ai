"""Retention settings routes (Task 17A).

Only the owner reads and sets the purge windows; admins and members are
denied. Values carry the same validation as the service (>= 1 day), and
Pydantic rejects anything else before the service is reached.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import AccessDenied, ProtectedResource, authorize
from source_catalog.retention import get_retention_policy, update_retention_policy

router = APIRouter(prefix="/api")


class RetentionUpdate(BaseModel):
    superseded_days: int = Field(ge=1)
    conversation_days: int = Field(ge=1)


def _policy_payload(superseded_days: int, conversation_days: int) -> dict:
    return {
        "superseded_days": superseded_days,
        "conversation_days": conversation_days,
    }


def _require_owner(context) -> None:
    try:
        authorize("retention.update", ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="owner role required") from None


@router.get("/settings/retention")
async def show(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    context = await catalog_context(user)
    _require_owner(context)
    policy = await get_retention_policy(uow(), workspace_id=context.workspace_id)
    return _policy_payload(policy.superseded_days, policy.conversation_days)


@router.put("/settings/retention")
async def update(
    payload: RetentionUpdate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_owner(context)
    policy = await update_retention_policy(
        uow(), EventWriter(),
        context=context,
        superseded_days=payload.superseded_days,
        conversation_days=payload.conversation_days,
    )
    return _policy_payload(policy.superseded_days, policy.conversation_days)
