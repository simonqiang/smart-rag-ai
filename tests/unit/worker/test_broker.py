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
