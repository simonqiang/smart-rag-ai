"""Scaffold smoke tests (Task 3): the monorepo packages are in place."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_api_and_worker_packages_are_importable() -> None:
    import apps.api
    import apps.worker

    assert apps.api.__doc__
    assert apps.worker.__doc__


def test_web_package_declares_a_production_build() -> None:
    manifest = json.loads((ROOT / "apps" / "web" / "package.json").read_text())
    assert "build" in manifest["scripts"]
    assert "test" in manifest["scripts"]
