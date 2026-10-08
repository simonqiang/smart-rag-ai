"""Upload routes (Task 9b).

Multipart bodies stream through a capped read loop so an oversized or
truncated transfer fails with an actionable reason instead of buffering
forever. Validation and storage live in ``ingestion.uploads``; this layer
maps machine reasons to statuses (413 over-limit, 400 bad content) and
reuses the catalog's 404/403 handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import ProtectedResource, authorize
from ingestion.uploads import REJECTION_MESSAGES, UploadRejected, register_upload

router = APIRouter(prefix="/api")

_READ_CHUNK = 1024 * 1024


def _limit_bytes() -> int:
    return Settings.load().limits.upload_mb * 1024 * 1024


def _store() -> ObjectStore:
    return ObjectStore(Path(Settings.load().data_dir))


async def read_capped(file: UploadFile, limit: int) -> bytes:
    """Stream a multipart body under the size cap (shared by upload routes)."""
    data = bytearray()
    try:
        while chunk := await file.read(_READ_CHUNK):
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(
                    status_code=413, detail=REJECTION_MESSAGES["over_limit"]
                )
    except OSError:
        raise HTTPException(
            status_code=400, detail="upload interrupted; please retry"
        ) from None
    return bytes(data)


@router.post("/uploads", status_code=201)
async def upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    collection_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.create", ProtectedResource(context.workspace_id), context)

    limit = _limit_bytes()
    data = await read_capped(file, limit)
    try:
        accepted = await register_upload(
            uow(),
            EventWriter(),
            _store(),
            context=context,
            collection_id=collection_id,
            name=name,
            filename=file.filename or "upload.bin",
            data=data,
            max_bytes=limit,
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
