"""Alembic environment: async engine driven by SMART_RAG_DATABASE_URL."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import DEFAULT_DATABASE_URL

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _configure(connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_url(), poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(_configure)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


def _url() -> str:
    return os.environ.get("SMART_RAG_DATABASE_URL") or DEFAULT_DATABASE_URL


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
