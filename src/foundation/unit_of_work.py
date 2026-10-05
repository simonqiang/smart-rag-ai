"""Transactional unit of work (Task 5).

``UnitOfWork.transaction()`` yields a SQLAlchemy async connection wrapped in
a database transaction: committing on clean exit, rolling back on any
exception. All writers (jobs, events) participate in the caller's
transaction, so state changes and their events are atomic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class UnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.begin() as connection:
            yield connection
