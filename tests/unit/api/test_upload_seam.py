"""Task 9b: the upload store seam resolves to the configured data dir.

Lives outside tests/api so the upload-route fixture's ``_store`` monkeypatch
does not hide the real implementation from coverage.
"""

from pathlib import Path

from apps.api.routes.uploads import ObjectStore, _store
from foundation.config import Settings


def test_store_seam_builds_object_store_under_data_dir() -> None:
    store = _store()
    root = Path(Settings.load().data_dir)
    assert (root / "objects").is_dir()
    assert (root / "manifests").is_dir()
    assert isinstance(store, ObjectStore)
