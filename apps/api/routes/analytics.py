"""Analytics routes (Task 25).

``POST /api/feedback`` lets any signed-in user flag one of their own stored
answers helpful or not (idempotent last-write-wins). ``GET /api/analytics/summary``
is the owner/admin aggregate view — counts only, never question or answer
text — reusing the ``operations.read`` gate from Task 22.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apps.api.routes.auth import uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import (
    AccessContext,
    AccessDenied,
    ProtectedResource,
    authorize,
)
from operations.feedback import FeedbackNotFound, analytics_summary, record_feedback

router = APIRouter(prefix="/api")


def _require_admin(context: AccessContext) -> None:
    try:
        authorize("operations.read", ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="admin role required") from None


class FeedbackRequest(BaseModel):
    request_id: str
    helpful: bool


@router.post("/feedback")
async def feedback(
    body: FeedbackRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    context = await catalog_context(user)
    try:
        await record_feedback(
            uow(), context=context, request_id=body.request_id, helpful=body.helpful
        )
    except FeedbackNotFound:
        raise HTTPException(status_code=404, detail="no such answer") from None
    return {"recorded": True}


@router.get("/analytics/summary")
async def summary(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict[str, int]:
    _require_admin(await catalog_context(user))
    return await analytics_summary(uow(), context=await catalog_context(user))
