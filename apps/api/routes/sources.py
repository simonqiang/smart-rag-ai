"""Source catalog routes (Task 8b).

Listing is grant-filtered by the catalog service; direct-ID lookups return
the generic 404 via the ``SourceNotFound`` handler in ``main``. Contexts
here carry real grants (``catalog_context``), unlike the role-only admin
contexts in ``users.py``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from apps.api.routes.auth import _check_origin, uow
from apps.api.routes.users import ready_user
from foundation.events import EventWriter
from identity_access.auth import AuthenticatedUser
from identity_access.authorization import (
    AccessContext,
    ProtectedResource,
    accessible_collection_ids,
    authorize,
)
from source_catalog.catalog import (
    create_collection,
    create_source,
    get_source,
    list_collections,
    list_sources,
)
from source_catalog.deletion import request_permanent_deletion

router = APIRouter(prefix="/api")


async def catalog_context(user: AuthenticatedUser) -> AccessContext:
    """Session context with the user's permitted collections resolved."""
    permitted = await accessible_collection_ids(
        uow(), user_id=user.user_id, workspace_id=user.workspace_id
    )
    return AccessContext(
        user_id=user.user_id,
        workspace_id=user.workspace_id,
        role=user.role,
        collection_ids=permitted,
    )


class CollectionRequest(BaseModel):
    name: str


class SourceRequest(BaseModel):
    collection_id: str
    name: str


@router.get("/collections")
async def collections_index(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    return [
        asdict(collection)
        for collection in await list_collections(
            uow(), context=await catalog_context(user)
        )
    ]


@router.post("/collections", status_code=201)
async def create_collection_route(
    request: Request,
    body: CollectionRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("collection.create", ProtectedResource(context.workspace_id), context)
    created = await create_collection(uow(), EventWriter(), context=context, name=body.name)
    return asdict(created)


@router.get("/sources")
async def sources_index(
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> list[dict]:
    return [
        asdict(source)
        for source in await list_sources(uow(), context=await catalog_context(user))
    ]


@router.get("/sources/{source_id}")
async def source_detail(
    source_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    source = await get_source(
        uow(), context=await catalog_context(user), source_id=source_id
    )
    return asdict(source)


@router.delete("/sources/{source_id}", status_code=202)
async def request_source_deletion(
    request: Request,
    source_id: str,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    """Owner-only tombstone; the worker purge is scheduled via the outbox."""
    _check_origin(request)
    context = await catalog_context(user)
    await request_permanent_deletion(
        uow(), EventWriter(), context=context, source_id=source_id
    )
    return {"source_id": source_id, "state": "deleted", "deletion": "scheduled"}


@router.post("/sources", status_code=201)
async def create_source_route(
    request: Request,
    body: SourceRequest,
    user: Annotated[AuthenticatedUser, Depends(ready_user)],
) -> dict:
    _check_origin(request)
    context = await catalog_context(user)
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    created = await create_source(
        uow(),
        EventWriter(),
        context=context,
        collection_id=body.collection_id,
        name=body.name,
    )
    return asdict(created)
