"""Dramatiq worker composition root (Task 4).

The Redis broker is built from settings; job records, outbox dispatch, and
leases arrive with Task 5. Dramatiq's CLI owns worker lifecycle and handles
SIGTERM by draining in-flight messages.
"""

from __future__ import annotations

import dramatiq

from apps.worker.broker import build_broker

# dramatiq materializes its own default RedisBroker (localhost, unconfigured)
# before this module loads; always replace it with the settings-configured one.
dramatiq.set_broker(build_broker())

broker = dramatiq.get_broker()

# Importing registers the actors with the broker.
from apps.worker.tasks import (
    extract,  # noqa: F401
    index,  # noqa: F401
)


@dramatiq.actor(max_retries=0)
def ping() -> str:
    return "pong"
