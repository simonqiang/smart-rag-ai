"""Index generation rebuild routes (Task 18).

The owner starts a rebuild when the embedding compatibility key changes and
watches its convergence; the worker stages and cuts over automatically.
Members and admins may observe but not start.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.config import Settings
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import AccessDenied, ProtectedResource, authorize
from knowledge_index.rebuild import RebuildError, rebuild_status, start_rebuild

router = APIRouter(prefix="/api")


@router.post("/index/rebuild", status_code=202)
async def start(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    try:
        authorize("index.rebuild", ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="owner role required") from None
    try:
        rebuild = await start_rebuild(
            uow(), EventWriter(),
            workspace_id=context.workspace_id, profile=Settings.load().active_profile,
        )
    except RebuildError:
        # start_rebuild refuses only when a rebuild is already running.
        raise HTTPException(
            status_code=409, detail="rebuild already running",
        ) from None
    return {
        "id": rebuild.id,
        "state": rebuild.state,
        "target_key": rebuild.target_key,
        "dimension": rebuild.dimension,
    }


@router.get("/index/rebuild")
async def status(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    context = await catalog_context(user)
    try:
        authorize("source.update", ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="admin role required") from None
    current = await rebuild_status(uow(), workspace_id=context.workspace_id)
    if current is None:
        return {"state": "idle"}
    return current
