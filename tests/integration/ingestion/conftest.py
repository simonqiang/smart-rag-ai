"""Shared fixtures for ingestion integration tests (live Compose PostgreSQL)."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings

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
