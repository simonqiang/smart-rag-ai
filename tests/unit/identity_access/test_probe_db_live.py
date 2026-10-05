import asyncio

import pytest

from foundation.config import Settings
from identity_access.setup import _probe_database


def test_probe_database_success_covers_reachable_line() -> None:
    ok, detail = asyncio.run(_probe_database(Settings.load()))
    if not ok:
        pytest.skip("Compose PostgreSQL not running (make up first)")
    assert (ok, detail) == (True, "reachable")
