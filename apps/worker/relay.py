"""Outbox relay (Task 5 composition).

Relays committed ``outbox`` rows to the broker in a one-second poll loop —
the dispatch half of "jobs dispatch only from committed PostgreSQL outbox
rows". Runs as its own container so a relay crash never touches worker
consumption and vice versa.

# ponytail: 1s polling instead of LISTEN/NOTIFY — imperceptible locally;
# switch to NOTIFY when dispatch latency matters at pilot scale.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from apps.worker.broker import build_broker
from foundation.config import Settings
from foundation.jobs import JobCommand, JobDispatcher, JobQueue

POLL_SECONDS = 1.0

logger = logging.getLogger("apps.worker.relay")


async def schedule_daily_retention_purge(engine: AsyncEngine) -> date:
    """Enqueue today's retention purge from a committed outbox row.

    The idempotency key makes this once per UTC day no matter how often the
    relay restarts; the job itself is idempotent (Task 17A).
    """
    today = datetime.now(UTC).date()
    async with engine.begin() as connection:
        await JobQueue().enqueue(
            JobCommand(
                type="retention_purge",
                payload={},
                idempotency_key=f"retention_purge:{today.isoformat()}",
            ),
            connection,
        )
    return today


async def _loop(engine, broker) -> None:
    dispatcher = JobDispatcher(engine, broker)
    scheduled: date | None = None
    while True:
        try:
            # CancelledError is a BaseException: it passes the handler below
            # and shuts the loop down cleanly.
            await dispatcher.relay()
        except Exception:
            logger.exception("outbox relay failed; retrying")
        # Scheduling never gates dispatch: a failing clock enqueue retries
        # next tick (the idempotency key keeps it once per day).
        try:
            if datetime.now(UTC).date() != scheduled:
                scheduled = await schedule_daily_retention_purge(engine)
        except Exception:
            logger.exception("daily retention scheduling failed; retrying next tick")
        await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = Settings.load()
    engine = create_async_engine(settings.database_url)
    logger.info("outbox relay started")
    try:
        asyncio.run(_loop(engine, build_broker()))
    except KeyboardInterrupt:
        logger.info("outbox relay stopped")


if __name__ == "__main__":
    main()
