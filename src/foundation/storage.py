"""Manifest-addressed object storage and backup-inventory port (Task 5).

``ObjectStore`` stores immutable bytes under their SHA-256 address with a
sidecar manifest; it is the local-filesystem adapter for the interface a
later S3 adapter will implement. ``ManagedBackupInventory`` is the port the
deletion path (Task 17) and the real backup implementation (Task 24) share.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


class ObjectStore:
    def __init__(self, root) -> None:
        self._root = root
        (root / "objects").mkdir(parents=True, exist_ok=True)
        (root / "manifests").mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        address = hashlib.sha256(data).hexdigest()
        object_path = self._root / "objects" / address
        if not object_path.exists():
            object_path.write_bytes(data)
        self._write_manifest(address, data)
        return address

    def get(self, address: str) -> bytes:
        data = (self._root / "objects" / address).read_bytes()
        if hashlib.sha256(data).hexdigest() != address:
            raise ValueError(f"checksum mismatch for object {address[:12]}")
        return data

    def manifest(self, address: str) -> dict:
        return json.loads((self._root / "manifests" / f"{address}.json").read_text())

    def delete(self, address: str) -> None:
        (self._root / "manifests" / f"{address}.json").unlink(missing_ok=True)
        (self._root / "objects" / address).unlink(missing_ok=True)

    def _write_manifest(self, address: str, data: bytes) -> None:
        manifest = {
            "sha256": address,
            "size": len(data),
            "stored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (self._root / "manifests" / f"{address}.json").write_text(json.dumps(manifest))


@dataclass(frozen=True)
class ManagedBackup:
    id: str
    path: str
    created_at: datetime
    source_ids: frozenset[str] = field(default_factory=frozenset)


class ManagedBackupInventory(Protocol):
    def list_containing(self, source_id: str) -> list[ManagedBackup]: ...

    def purge_containing(self, source_id: str) -> int: ...


class InMemoryManagedBackupInventory:
    """Test fake for the deletion-aware inventory; Task 24 implements storage."""

    def __init__(self, backups: list[ManagedBackup] | None = None) -> None:
        self._backups = list(backups or [])

    def list_containing(self, source_id: str) -> list[ManagedBackup]:
        return [backup for backup in self._backups if source_id in backup.source_ids]

    def purge_containing(self, source_id: str) -> int:
        doomed = self.list_containing(source_id)
        self._backups = [backup for backup in self._backups if backup not in doomed]
        return len(doomed)
