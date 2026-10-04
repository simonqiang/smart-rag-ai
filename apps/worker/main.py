"""Dramatiq worker composition root (Task 4).

The Redis broker is built from settings; job records, outbox dispatch, and
leases arrive with Task 5. Dramatiq's CLI owns worker lifecycle and handles
SIGTERM by draining in-flight messages.
"""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from foundation.config import Settings

_settings = Settings.load()

if not isinstance(dramatiq.get_broker(), RedisBroker):
    dramatiq.set_broker(RedisBroker(url=_settings.redis_url))

broker = dramatiq.get_broker()


@dramatiq.actor(max_retries=0)
def ping() -> str:
    return "pong"
