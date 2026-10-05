"""Default probe success paths against the live Compose stack.

Skips cleanly when the stack is down; hermetic unit tests cover the failure
paths in test_setup_dependencies.py.
"""

import asyncio

import pytest

from apps.api.routes import auth as auth_routes
from foundation.config import Settings
from identity_access.setup import _probe_database, _probe_redis


def test_probe_database_success_covers_reachable_line() -> None:
    ok, detail = asyncio.run(_probe_database(Settings.load()))
    if not ok:
        pytest.skip("Compose PostgreSQL not running (make up first)")
    assert (ok, detail) == (True, "reachable")


def test_probe_redis_success_covers_reachable_line() -> None:
    ok, detail = asyncio.run(_probe_redis(Settings.load()))
    if not ok:
        pytest.skip("Compose Redis not running (make up first)")
    assert (ok, detail) == (True, "reachable")


def test_route_readiness_aggregates_live_checks() -> None:
    readiness = asyncio.run(auth_routes.readiness())
    if not readiness.ok:
        pytest.skip("Compose stack not fully running (make up first)")
    assert [check.name for check in readiness.checks] == [
        "database",
        "redis",
        "storage",
        "ollama",
    ]
