"""Source catalog service: collections and source records (Task 8a).

Listing is filtered by ``accessible_collection_ids``: members only see
sources in their granted collections, owners/admins see every collection
in the workspace. Direct-ID lookups deny unknown, cross-workspace, and
ungranted sources with the same ``SourceNotFound`` so IDs cannot be
probed (mirrors the Task 7 user-administration pattern).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import (
    AccessContext,
    ProtectedResource,
    accessible_collection_ids,
    authorize,
    can_access_collection,
)


@dataclass(frozen=True)
class CollectionDTO:
    collection_id: str
    name: str
    created_at: datetime


@dataclass(frozen=True)
class SourceDTO:
    source_id: str
    collection_id: str
    name: str
    state: str
    created_at: datetime


class CollectionNotFound(Exception):
    """Unknown collection or one belonging to another workspace."""


class SourceNotFound(Exception):
    """Unknown source, another workspace's, or not granted to the caller."""


async def create_collection(
    uow: UnitOfWork, writer: EventWriter, *, context: AccessContext, name: str
) -> CollectionDTO:
    authorize("collection.create", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text(
                        "SELECT id, created_at FROM collections "
                        "WHERE workspace_id = :workspace_id AND name = :name"
                    ),
                    {"workspace_id": context.workspace_id, "name": name},
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            return CollectionDTO(
                collection_id=str(row["id"]),
                name=name,
                created_at=row["created_at"],
            )
        collection_id = str(uuid.uuid4())
        created_at = (
            await transaction.execute(
                text(
                    "INSERT INTO collections (id, workspace_id, name) "
                    "VALUES (:id, :workspace_id, :name) RETURNING created_at"
                ),
                {"id": collection_id, "workspace_id": context.workspace_id, "name": name},
            )
        ).scalar_one()
        await writer.record(
            AuditEvent(
                type="collection.created",
                actor=context.user_id,
                subject=collection_id,
                metadata={"name": name},
            ),
            transaction,
        )
    return CollectionDTO(collection_id=collection_id, name=name, created_at=created_at)


async def create_source(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    context: AccessContext,
    collection_id: str,
    name: str,
) -> SourceDTO:
    authorize("source.create", ProtectedResource(context.workspace_id), context)
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text(
                        "SELECT workspace_id FROM collections WHERE id = :id"
                    ),
                    {"id": collection_id},
                )
            )
            .scalar_one_or_none()
        )
        if row is None or str(row) != context.workspace_id:
            raise CollectionNotFound(collection_id)
        source_id = str(uuid.uuid4())
        created_at = (
            await transaction.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, collection_id, name, "
                    "state, created_by) VALUES (:id, :workspace_id, :collection_id, "
                    ":name, 'active', :created_by) RETURNING created_at"
                ),
                {
                    "id": source_id,
                    "workspace_id": context.workspace_id,
                    "collection_id": collection_id,
                    "name": name,
                    "created_by": context.user_id,
                },
            )
        ).scalar_one()
        await writer.record(
            AuditEvent(
                type="source.created",
                actor=context.user_id,
                subject=source_id,
                metadata={"name": name, "collection_id": collection_id},
            ),
            transaction,
        )
    return SourceDTO(
        source_id=source_id,
        collection_id=collection_id,
        name=name,
        state="active",
        created_at=created_at,
    )


async def list_collections(uow: UnitOfWork, *, context: AccessContext) -> list[CollectionDTO]:
    permitted = await accessible_collection_ids(
        uow, user_id=context.user_id, workspace_id=context.workspace_id
    )
    async with uow.transaction() as transaction:
        rows = (
            await transaction.execute(
                text(
                    "SELECT id, name, created_at FROM collections "
                    "WHERE workspace_id = :workspace_id "
                    "AND id = ANY(CAST(:ids AS uuid[])) ORDER BY name"
                ),
                {"workspace_id": context.workspace_id, "ids": permitted},
            )
        )
        return [
            CollectionDTO(
                collection_id=str(row["id"]), name=str(row["name"]), created_at=row["created_at"]
            )
            for row in rows.mappings()
        ]


async def list_sources(uow: UnitOfWork, *, context: AccessContext) -> list[SourceDTO]:
    permitted = await accessible_collection_ids(
        uow, user_id=context.user_id, workspace_id=context.workspace_id
    )
    async with uow.transaction() as transaction:
        rows = (
            await transaction.execute(
                text(
                    "SELECT id, collection_id, name, state, created_at FROM sources "
                    "WHERE workspace_id = :workspace_id "
                    "AND collection_id = ANY(CAST(:ids AS uuid[])) "
                    "ORDER BY created_at, id"
                ),
                {"workspace_id": context.workspace_id, "ids": permitted},
            )
        )
        return [
            SourceDTO(
                source_id=str(row["id"]),
                collection_id=str(row["collection_id"]),
                name=str(row["name"]),
                state=str(row["state"]),
                created_at=row["created_at"],
            )
            for row in rows.mappings()
        ]


async def get_source(
    uow: UnitOfWork, *, context: AccessContext, source_id: str
) -> SourceDTO:
    permitted = await accessible_collection_ids(
        uow, user_id=context.user_id, workspace_id=context.workspace_id
    )
    scoped = AccessContext(
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        role=context.role,
        collection_ids=permitted,
    )
    async with uow.transaction() as transaction:
        row = (
            (
                await transaction.execute(
                    text(
                        "SELECT id, collection_id, name, state, created_at FROM sources "
                        "WHERE id = :id AND workspace_id = :workspace_id"
                    ),
                    {"id": source_id, "workspace_id": context.workspace_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None or not can_access_collection(scoped, str(row["collection_id"])):
        raise SourceNotFound(source_id)
    return SourceDTO(
        source_id=str(row["id"]),
        collection_id=str(row["collection_id"]),
        name=str(row["name"]),
        state=str(row["state"]),
        created_at=row["created_at"],
    )
