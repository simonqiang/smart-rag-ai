"""Source version lifecycle routes (Task 16).

Archive/unarchive, replacement staging, validated cutover, and
rollback-as-new-version. Admin-gated transitions return 403 for members,
unknown/foreign IDs the generic 404, and a version that is not in the
state a transition requires a 409 conflict.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.uploads import _limit_bytes, _store, read_capped
from apps.api.routes.users import ready_user
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import ProtectedResource, authorize
from ingestion.uploads import REJECTION_MESSAGES, UploadRejected
from source_catalog.version_service import (
    activate_ready_version,
    archive_source,
    list_versions,
    rollback_as_new_version,
    stage_replacement,
    unarchive_source,
)

router = APIRouter(prefix="/api")


@router.get("/sources/{source_id}/versions")
async def version_history(
    source_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    return [
        asdict(version)
        for version in await list_versions(
            uow(), context=await catalog_context(user), source_id=source_id
        )
    ]


@router.post("/sources/{source_id}/archive")
async def archive(
    request: Request,
    source_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    await archive_source(uow(), EventWriter(), context=context, source_id=source_id)
    return {"source_id": source_id, "state": "archived"}


@router.post("/sources/{source_id}/unarchive")
async def unarchive(
    request: Request,
    source_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    await unarchive_source(uow(), EventWriter(), context=context, source_id=source_id)
    return {"source_id": source_id, "state": "active"}


@router.post("/sources/{source_id}/versions", status_code=201)
async def stage_version(
    request: Request,
    source_id: str,
    file: Annotated[UploadFile, File()],
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    limit = _limit_bytes()
    data = await read_capped(file, limit)
    try:
        accepted = await stage_replacement(
            uow(), EventWriter(), _store(),
            context=context, source_id=source_id,
            filename=file.filename or "replacement.bin", data=data,
        )
    except UploadRejected as rejected:
        status = 413 if rejected.reason == "over_limit" else 400
        raise HTTPException(
            status_code=status, detail=REJECTION_MESSAGES[rejected.reason]
        ) from None
    return {
        "source_id": accepted.source_id,
        "source_version_id": accepted.source_version_id,
        "checksum": accepted.checksum,
        "size": accepted.size,
        "media_type": accepted.media_type,
        "duplicate": accepted.duplicate,
    }


@router.post("/sources/{source_id}/versions/{version_id}/activate")
async def activate(
    request: Request,
    source_id: str,
    version_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    await activate_ready_version(
        uow(), EventWriter(), context=context, source_id=source_id, version_id=version_id,
    )
    return {"source_id": source_id, "active_version_id": version_id}


@router.post("/sources/{source_id}/versions/{version_id}/rollback", status_code=201)
async def rollback(
    request: Request,
    source_id: str,
    version_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.update", ProtectedResource(context.workspace_id), context)
    restored = await rollback_as_new_version(
        uow(), EventWriter(), context=context, source_id=source_id, version_id=version_id,
    )
    return asdict(restored)
