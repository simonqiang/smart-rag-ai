"""Website sync routes (Task 21b).

Creating, run-now, and enable/disable are owner/admin actions on the
schedule's workspace; the freshness list is grant-scoped through
``catalog_context`` (members see only their collections). Unknown and
cross-workspace schedule IDs return the identical generic 404 via the
``ScheduleNotFound`` handler in ``main``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import AccessDenied, ProtectedResource, authorize
from source_catalog.catalog import CollectionNotFound
from web_sync.scheduler import (
    MIN_INTERVAL_SECONDS,
    ScheduleDisabled,
    ScheduleNotFound,
    SyncSchedule,
    list_schedules,
    request_sync,
    schedule_sync,
    set_schedule_enabled,
)

router = APIRouter(prefix="/api/web-sync")


class WebSyncRequest(BaseModel):
    collection_id: str
    name: str
    start_url: str
    sitemap_url: str | None = None
    allowed_prefixes: list[str] = Field(default_factory=list)
    interval_seconds: int = Field(default=86_400, ge=MIN_INTERVAL_SECONDS)


class EnabledUpdate(BaseModel):
    enabled: bool


def _payload(schedule: SyncSchedule) -> dict:
    return {
        "schedule_id": schedule.schedule_id,
        "collection_id": schedule.collection_id,
        "source_id": schedule.source_id,
        "name": schedule.name,
        "start_url": schedule.start_url,
        "sitemap_url": schedule.sitemap_url,
        "allowed_prefixes": schedule.allowed_prefixes,
        "interval_seconds": schedule.interval_seconds,
        "enabled": schedule.enabled,
        "next_sync_at": schedule.next_sync_at,
        "last_synced_at": schedule.last_synced_at,
        "last_state": schedule.last_state,
        "last_detail": schedule.last_detail,
    }


def _require_admin(context) -> None:
    try:
        authorize("source.update", ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="admin role required") from None


@router.post("", status_code=201)
async def create(
    request: Request,
    body: WebSyncRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    try:
        schedule = await schedule_sync(
            uow(), EventWriter(),
            context=context,
            collection_id=body.collection_id,
            name=body.name,
            start_url=body.start_url,
            sitemap_url=body.sitemap_url,
            allowed_prefixes=body.allowed_prefixes or None,
            interval_seconds=body.interval_seconds,
            now=datetime.now(UTC),
        )
    except CollectionNotFound:
        raise HTTPException(status_code=404, detail="no such collection") from None
    return _payload(schedule)


@router.get("")
async def freshness(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    context = await catalog_context(user)
    schedules = await list_schedules(
        uow(), workspace_id=context.workspace_id, collection_ids=context.collection_ids,
    )
    return [_payload(schedule) for schedule in schedules]


@router.post("/{schedule_id}/run", status_code=202)
async def run_now(
    schedule_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_admin(context)
    try:
        await request_sync(
            uow(), EventWriter(), context=context, schedule_id=schedule_id,
            now=datetime.now(UTC),
        )
    except ScheduleDisabled:
        raise HTTPException(
            status_code=409, detail="schedule is disabled",
        ) from None
    return {"schedule_id": schedule_id, "sync": "requested"}


@router.put("/{schedule_id}/enabled")
async def set_enabled(
    schedule_id: str,
    body: EnabledUpdate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_admin(context)
    try:
        schedule = await set_schedule_enabled(
            uow(), EventWriter(), context=context, schedule_id=schedule_id,
            enabled=body.enabled, now=datetime.now(UTC),
        )
    except ScheduleNotFound:
        raise HTTPException(status_code=404, detail="no such schedule") from None
    return _payload(schedule)
