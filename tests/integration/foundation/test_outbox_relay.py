"""Outbox relay composition: committed rows reach the broker (Task 5).

The relay is the dispatch half of the job pipeline; without it, committed
outbox rows queue forever (found live in Checkpoint B). These tests spy on
the broker instead of reading Redis: the live stack's worker consumes the
shared queue within one poll period, so queue depth is a race.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.jobs import JobCommand, JobQueue
from foundation.unit_of_work import UnitOfWork

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module", autouse=True)
def _migrated(settings: Settings):
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE jobs, outbox, audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


class RecordingBroker:
    def __init__(self) -> None:
        self.messages: list = []

    def enqueue(self, message) -> None:
        self.messages.append(message)


def test_relay_publishes_committed_outbox_rows(db: Settings) -> None:
    async def scenario() -> tuple[list, int]:
        from apps.worker.relay import POLL_SECONDS, _loop

        engine = create_async_engine(db.database_url)
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE jobs, outbox"))
        broker = RecordingBroker()

        async with UnitOfWork(engine).transaction() as transaction:
            await JobQueue().enqueue(
                JobCommand(type="source_extract", payload={"version_id": "v1"}),
                transaction,
            )

        loop_task = asyncio.create_task(_loop(engine, broker))
        try:
            await asyncio.sleep(POLL_SECONDS * 2)
            async with engine.begin() as connection:
                unpublished = (
                    await connection.execute(
                        text("SELECT count(*) FROM outbox WHERE published_at IS NULL")
                    )
                ).scalar_one()
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            await engine.dispose()
        return broker.messages, unpublished

    messages, unpublished = asyncio.run(scenario())
    # The relay published the committed row and marked it; the topic is the
    # actor name, which the real actors (`source_extract`, `source_index`) match.
    assert [message.actor_name for message in messages] == ["source_extract"]
    assert messages[0].kwargs["version_id"] == "v1"  # job_id added by the queue
    assert unpublished == 0


def test_relay_survives_outage_and_recovers(db: Settings) -> None:
    """A failure inside the loop must not kill the relay."""

    async def scenario() -> list:
        from apps.worker.relay import POLL_SECONDS, _loop

        engine = create_async_engine(db.database_url)
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE jobs, outbox"))

        class ExplodingBroker:
            def enqueue(self, message) -> None:
                raise RuntimeError("broker down")

        broken = ExplodingBroker()
        healed = RecordingBroker()
        loop_task = asyncio.create_task(_loop(engine, broken))
        try:
            async with UnitOfWork(engine).transaction() as transaction:
                await JobQueue().enqueue(
                    JobCommand(type="source_extract", payload={}), transaction
                )
            await asyncio.sleep(POLL_SECONDS * 2)  # first ticks fail...
            # Heal: swap the broker and let a later tick publish.
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            loop_task = asyncio.create_task(_loop(engine, healed))
            await asyncio.sleep(POLL_SECONDS * 2)
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            await engine.dispose()
        return healed.messages

    messages = asyncio.run(scenario())
    # The outbox row was never marked, so the healed relay publishes it.
    assert [message.actor_name for message in messages] == ["source_extract"]
