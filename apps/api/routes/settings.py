"""Retention and backup settings routes (Tasks 17A, 24c).

Only the owner reads and sets the purge windows and manages managed backups;
admins and members are denied. Values carry the same validation as the
service (>= 1 day), and Pydantic rejects anything else before the service is
reached. Creating a backup reads the recovery key from the environment at
call time (project secret rule) — a missing key is an actionable 400.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from apps.api.routes.auth import _check_origin, engine, uow
from apps.api.routes.sources import catalog_context
from apps.api.routes.users import ready_user
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import AccessDenied, ProtectedResource, authorize
from operations.backup import BackupKeyError, create_backup
from source_catalog.retention import get_retention_policy, update_retention_policy

router = APIRouter(prefix="/api")


class RetentionUpdate(BaseModel):
    superseded_days: int = Field(ge=1)
    conversation_days: int = Field(ge=1)
    backup_days: int = Field(ge=1)


def _policy_payload(policy) -> dict:
    return {
        "superseded_days": policy.superseded_days,
        "conversation_days": policy.conversation_days,
        "backup_days": policy.backup_days,
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
    return _policy_payload(
        await get_retention_policy(uow(), workspace_id=context.workspace_id)
    )


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
        backup_days=payload.backup_days,
    )
    return _policy_payload(policy)


@router.get("/settings/backup")
async def list_backups(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    context = await catalog_context(user)
    _require_owner(context)
    async with engine().begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT id, filename, size_bytes, created_at, "
                    "jsonb_array_length(source_ids) AS source_count "
                    "FROM managed_backups ORDER BY created_at DESC"
                )
            )
        ).mappings().all()
    return {
        "backups": [
            {
                "id": str(row["id"]),
                "filename": str(row["filename"]),
                "size_bytes": int(row["size_bytes"]),
                "created_at": row["created_at"].isoformat(),
                "source_count": int(row["source_count"]),
            }
            for row in rows
        ]
    }


@router.post("/settings/backup", status_code=201)
async def create(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    _require_owner(context)
    settings = Settings.load()
    try:
        record = await create_backup(
            engine(), ObjectStore(Path(settings.data_dir)), EventWriter(),
            backup_dir=Path(settings.backup_dir), actor=context.user_id,
        )
    except BackupKeyError as failure:
        raise HTTPException(status_code=400, detail=str(failure)) from None
    return {
        "id": str(record.id),
        "filename": record.filename,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
    }
