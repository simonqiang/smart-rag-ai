"""Shared fixtures for ingestion integration tests (live Compose PostgreSQL)."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from identity_access.setup import create_first_owner
from source_catalog.catalog import create_collection

INGESTION_TABLES = (
    "source_versions, sources, collections, invitations, sessions, users, workspaces"
)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {INGESTION_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


@pytest.fixture()
def store_root(db: Settings, tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture()
def workspace(db: Settings, store_root) -> dict:
    """Owner plus one collection; objects are stored under ``store_root``."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        ObjectStore(store_root)
        try:
            owner = await create_first_owner(
                uow, EventWriter(), email="owner@example.com", password="owner-password-1"
            )
            collection = await create_collection(
                uow, EventWriter(),
                context=AccessContext(
                    user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner"
                ),
                name="Policies",
            )
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id, "owner_id": owner.user_id,
                "collection_id": collection.collection_id}

    return asyncio.run(create())
