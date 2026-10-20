"""Operations routes (Task 22b).

Owner/admin views over what the platform already recorded: job rows and
their leases, source health from the version lifecycle, the immutable audit
log, and the sanitized doctor report (spec §10: diagnostics redact secrets
and source content). ``retry`` re-dispatches a failed job through the
committed-outbox path — there is no direct-to-broker route.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.config import Settings
from foundation.doctor import run_doctor
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import (
    AccessContext,
    AccessDenied,
    ProtectedResource,
    authorize,
)
from operations.audit import list_audit_events
from operations.job_queries import (
    JobNotFound,
    JobNotRetryable,
    list_jobs,
    list_source_health,
    retry_job,
)

router = APIRouter(prefix="/api/operations")

EVENT_TYPE_PATTERN = r"[a-z][a-z0-9_.]{0,99}"


def _require(action: str, context: AccessContext) -> None:
    try:
        authorize(action, ProtectedResource(context.workspace_id), context)
    except AccessDenied:
        raise HTTPException(status_code=403, detail="admin role required") from None


def _page(items: list[dict], *, total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/jobs")
async def jobs(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
    status: str | None = None,
    job_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    _require("operations.read", await catalog_context(user))
    page = await list_jobs(
        uow(), status=status, job_type=job_type, limit=limit, offset=offset,
    )
    return _page(
        [
            {
                "job_id": job.job_id,
                "type": job.type,
                "status": job.status,
                "state": job.state,
                "attempts": job.attempts,
                "last_error": job.last_error,
                "retryable": job.retryable,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
            for job in page.items
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry(
    job_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require("operations.retry", context)
    try:
        await retry_job(uow(), EventWriter(), job_id, actor=context.user_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail="no such job") from None
    except JobNotRetryable:
        raise HTTPException(
            status_code=409, detail="only failed jobs can be retried",
        ) from None
    return {"job_id": job_id, "retry": "requested"}


@router.get("/sources/health")
async def source_health(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    _require("operations.read", await catalog_context(user))
    page = await list_source_health(uow(), limit=limit, offset=offset)
    return _page(
        [
            {
                "source_id": item.source_id,
                "name": item.name,
                "state": item.state,
                "latest_version_state": item.latest_version_state,
                "latest_version_at": item.latest_version_at,
                "created_at": item.created_at,
            }
            for item in page.items
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/audit")
async def audit(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
    event_type: Annotated[str | None, Query(pattern=EVENT_TYPE_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    _require("operations.read", await catalog_context(user))
    page = await list_audit_events(
        uow(), event_type=event_type, limit=limit, offset=offset,
    )
    return _page(
        [
            {
                "event_id": entry.event_id,
                "type": entry.type,
                "actor": entry.actor,
                "subject": entry.subject,
                "metadata": entry.metadata,
                "created_at": entry.created_at,
            }
            for entry in page.items
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/diagnostics")
def diagnostics(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    # Sync def on purpose: doctor probes block, so FastAPI runs this in a
    # worker thread and the event loop keeps serving.
    _require(
        "operations.read",
        AccessContext(user_id=user.user_id, workspace_id=user.workspace_id, role=user.role),
    )
    report = run_doctor(Settings.load())
    return {
        "host": report.host,
        "ok": report.ok,
        "checks": [
            {
                "name": check.name,
                "status": check.status.value,
                "detail": report.redact(check.detail),
                "remediation": (
                    report.redact(check.remediation)
                    if check.status.value != "ok" and check.remediation
                    else ""
                ),
            }
            for check in report.results
        ],
    }
