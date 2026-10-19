"""Shared fixtures for web_sync integration tests (live Compose PostgreSQL)."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, accessible_collection_ids
from identity_access.grants import set_user_collections
from identity_access.invitations import accept_invitation, issue_invitation_token
from identity_access.setup import create_first_owner
from source_catalog.catalog import create_collection

WEB_SYNC_TABLES = (
    "index_chunks, index_generations, source_versions, site_sync_schedules, sources, "
    "collections, invitations, sessions, users, workspaces, jobs, outbox"
)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {WEB_SYNC_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


@pytest.fixture()
def store_root(db: Settings, tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture()
def workspace(db: Settings, store_root: Path) -> dict:
    """Owner plus one collection; the objects are stored under ``store_root``."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(
                uow,
                EventWriter(),
                email="owner@example.com",
                password="owner-password-1",
            )
            collection = await create_collection(
                uow,
                EventWriter(),
                context=AccessContext(
                    user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner"
                ),
                name="Handbook",
            )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "collection_id": collection.collection_id,
        }

    return asyncio.run(create())


async def _member(uow: UnitOfWork, workspace: dict, email: str) -> dict:
    """Member via invitation; grants are set separately by the caller."""
    invite = await issue_invitation_token(
        uow, EventWriter(), workspace_id=workspace["workspace_id"],
        email=email, role="member", invited_by=workspace["owner_id"],
    )
    user = await accept_invitation(
        uow, EventWriter(), token=invite.token, password="member-password-1"
    )
    return {"user_id": user.user_id, "context": AccessContext(
        user_id=user.user_id, workspace_id=workspace["workspace_id"], role="member",
        collection_ids=await accessible_collection_ids(
            uow, user_id=user.user_id, workspace_id=workspace["workspace_id"]
        ),
    )}


@pytest.fixture()
def workspace_with_members(db: Settings, store_root: Path) -> dict:
    """Owner, one granted member, one ungranted member."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(
                uow, EventWriter(),
                email="owner@example.com", password="owner-password-1",
            )
            owner_context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner"
            )
            collection = await create_collection(
                uow, EventWriter(), context=owner_context, name="Handbook"
            )
            base = {
                "workspace_id": owner.workspace_id,
                "owner_id": owner.user_id,
                "owner_context": AccessContext(
                    user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
                    collection_ids=[collection.collection_id],
                ),
                "collection_id": collection.collection_id,
            }
            granted = await _member(uow, base, "granted@example.com")
            ungranted = await _member(uow, base, "ungranted@example.com")
            await set_user_collections(
                uow, EventWriter(), context=owner_context, target_user_id=granted["user_id"],
                collections=[base["collection_id"]],
            )
            # Grants commit before the contexts snapshot collection_ids.
            for member in (granted, ungranted):
                member["context"] = AccessContext(
                    user_id=member["user_id"], workspace_id=base["workspace_id"],
                    role="member",
                    collection_ids=await accessible_collection_ids(
                        uow, user_id=member["user_id"], workspace_id=base["workspace_id"]
                    ),
                )
        finally:
            await engine.dispose()
        return {**base, "granted": granted, "ungranted": ungranted}

    return asyncio.run(create())
