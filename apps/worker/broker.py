"""Shared worker broker construction.

Both the Dramatiq worker and the outbox relay must talk to the same
settings-configured Redis; dramatiq's own default broker (localhost) is
never acceptable (see the broker regression test).
"""

from __future__ import annotations

from dramatiq.brokers.redis import RedisBroker

from foundation.config import Settings


def build_broker() -> RedisBroker:
    return RedisBroker(url=Settings.load().redis_url)
