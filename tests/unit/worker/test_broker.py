"""Worker composition root: the broker must follow the configured Redis URL.

dramatiq materializes its own default RedisBroker (localhost, no options)
before the worker module is imported; the composition root has to replace
it instead of trusting an isinstance check.
"""

from __future__ import annotations

import importlib
import urllib.parse

import dramatiq

from foundation.config import Settings


def test_worker_broker_uses_configured_redis_url(monkeypatch) -> None:
    from dramatiq.brokers.redis import RedisBroker

    dramatiq.set_broker(RedisBroker())  # simulate the CLI's pre-import default

    import apps.worker.main as worker_main

    importlib.reload(worker_main)

    kwargs = dramatiq.get_broker().client.connection_pool.connection_kwargs
    parsed = urllib.parse.urlparse(Settings.load().redis_url)
    assert kwargs["host"] == parsed.hostname
    assert kwargs["port"] == parsed.port or 6379


def test_actor_engines_use_null_pool_for_loop_safety() -> None:
    # Actors run one asyncio.run per delivery; pooled connections would stay
    # bound to the first (closed) loop and crash on dispose.
    from sqlalchemy.pool import NullPool

    from apps.worker.tasks.extract import _engine as extract_engine
    from apps.worker.tasks.index import _engine as index_engine

    for build in (extract_engine, index_engine):
        engine = build("postgresql+asyncpg://user:pw@localhost/db")
        assert isinstance(engine.pool, NullPool)
