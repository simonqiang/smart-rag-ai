"""Task 6a: identity schema and the first-owner primitive.

Runs against the live Compose PostgreSQL (loopback port). Deterministic:
identity tables are truncated before each test, one event loop per test.
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.passwords import verify_password
from identity_access.setup import WorkspaceAlreadyInitialized, create_first_owner

ROOT = Path(__file__).resolve().parents[3]

IDENTITY_TABLES = "invitations, sessions, users, workspaces"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {IDENTITY_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


def _uow(settings: Settings) -> tuple[UnitOfWork, ...]:
    engine = create_async_engine(settings.database_url)
    return engine, UnitOfWork(engine), EventWriter()


def test_first_owner_creates_workspace_user_and_audit(db: Settings) -> None:
    async def scenario() -> dict:
        engine, uow, writer = _uow(db)
        try:
            created = await create_first_owner(
                uow, writer, email="owner@example.com", password="owner-password-1"
            )
            async with engine.connect() as connection:
                workspace = (
                    (
                        await connection.execute(
                            text("SELECT name FROM workspaces WHERE id = :id"),
                            {"id": created.workspace_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                user = (
                    (
                        await connection.execute(
                            text(
                                "SELECT email, role, status, must_change_password, "
                                "password_hash FROM users WHERE id = :id"
                            ),
                            {"id": created.user_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                audits = (
                    (
                        await connection.execute(
                            text(
                                "SELECT type, actor FROM audit_events "
                                "WHERE type = 'workspace.initialized'"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            return {
                "workspace": dict(workspace),
                "user": dict(user),
                "audits": [dict(a) for a in audits],
            }
        finally:
            await engine.dispose()

    result = asyncio.run(scenario())
    assert result["workspace"]["name"] == "Workspace"
    assert result["user"]["email"] == "owner@example.com"
    assert result["user"]["role"] == "owner"
    assert result["user"]["status"] == "active"
    assert result["user"]["must_change_password"] is False
    assert verify_password("owner-password-1", result["user"]["password_hash"])
    assert "owner-password-1" not in result["user"]["password_hash"]
    assert result["audits"] == [{"type": "workspace.initialized", "actor": "owner@example.com"}]


def test_second_owner_is_refused_and_persists_nothing(db: Settings) -> None:
    async def scenario() -> tuple[int, int]:
        engine, uow, writer = _uow(db)
        try:
            await create_first_owner(
                uow, writer, email="owner@example.com", password="owner-password-1"
            )
            with pytest.raises(WorkspaceAlreadyInitialized):
                await create_first_owner(
                    uow, writer, email="second@example.com", password="other-password-2"
                )
            async with engine.connect() as connection:
                users = (
                    await connection.execute(text("SELECT count(*) FROM users"))
                ).scalar_one()
                workspaces = (
                    await connection.execute(text("SELECT count(*) FROM workspaces"))
                ).scalar_one()
            return users, workspaces
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (1, 1)
