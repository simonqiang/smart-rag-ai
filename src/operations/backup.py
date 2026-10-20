"""Encrypted managed backups and the deletion-aware inventory (Task 24a).

One encrypted file per backup in ``backup_dir``:

    b"SRB1" | header length (u32 BE) | header JSON | ciphertext

The header is plaintext so ``inspect_backup`` reports counts and the schema
revision without the key; it is bound into the AES-GCM calculation as
associated data, so tampering with it fails decryption too. The payload is
a zip bundle (``manifest.json`` + ``tables/<name>.jsonl`` + ``objects/<sha>``)
whose manifest checksums every member; the header checksums the whole bundle.
The recovery key arrives through ``SMART_RAG_BACKUP_KEY`` at call time —
like the GLM key it is never stored — and scrypt derives the AES key from it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import uuid as uuid_module
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.events import AuditEvent, EventWriter
from foundation.storage import ManagedBackup

MAGIC = b"SRB1"
FORMAT_VERSION = 1
BUNDLE_FORMAT = "smart-rag-backup"
KEY_ENV = "SMART_RAG_BACKUP_KEY"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _KEY_LEN = 2**14, 8, 1, 32

# Active knowledge and permissions in FK-safe order; sessions, jobs, outbox,
# index_rebuilds, and managed_backups itself are ephemeral or target-local.
TABLES = (
    "workspaces", "users", "invitations", "collection_grants", "collections",
    "sources", "source_versions", "index_generations", "index_chunks",
    "conversations", "conversation_messages", "audit_events",
    "retention_policies", "site_sync_schedules", "generation_provider_settings",
)


class BackupKeyError(Exception):
    """The recovery key is missing, wrong, or the bundle fails authentication."""


class BackupCorrupt(Exception):
    """The file is not a readable backup (bad magic, truncation, checksum)."""


def read_recovery_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise BackupKeyError(
            f"set the {KEY_ENV} environment variable to the recovery key"
        )
    return key


def _derive_key(recovery_key: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        recovery_key.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _serialize(value):
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    if isinstance(value, (uuid_module.UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise ValueError(f"unsupported column type {type(value).__name__}")


@dataclass(frozen=True)
class BackupRecord:
    id: uuid_module.UUID
    filename: str
    sha256: str
    size_bytes: int
    counts: dict


@dataclass(frozen=True)
class BackupInfo:
    filename: str
    sha256: str
    size_bytes: int
    created_at: str
    format_version: int
    schema_revision: str
    counts: dict
    source_count: int


async def _dump(engine: AsyncEngine) -> tuple[dict[str, list[dict]], str, list[str]]:
    """Every data table as JSON-ready rows, plus the schema revision and
    the source ids the deletion-aware inventory needs."""
    tables: dict[str, list[dict]] = {}
    async with engine.begin() as connection:
        revision = str(
            (await connection.execute(text("SELECT version_num FROM alembic_version")))
            .scalar_one()
        )
        for table in TABLES:
            rows = (
                await connection.execute(text(f"SELECT * FROM {table}"))
            ).mappings().all()
            tables[table] = [
                {key: _serialize(value) for key, value in dict(row).items()}
                for row in rows
            ]
        source_ids = sorted(
            str(row[0]) for row in (await connection.execute(text("SELECT id FROM sources")))
        )
    return tables, revision, source_ids


def _bundle(
    tables: dict[str, list[dict]], revision: str, created_at: str, store,
) -> bytes:
    buffer = io.BytesIO()
    counts = {table: len(rows) for table, rows in tables.items()}
    members: dict[str, str] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        def add(name: str, data: bytes) -> None:
            archive.writestr(name, data)
            members[name] = hashlib.sha256(data).hexdigest()

        for table, rows in tables.items():
            payload = "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows
            )
            add(f"tables/{table}.jsonl", payload.encode())
        for address in store.addresses():
            add(f"objects/{address}", store.get(address))
        add(
            "manifest.json",
            json.dumps(
                {
                    "format": BUNDLE_FORMAT,
                    "format_version": FORMAT_VERSION,
                    "created_at": created_at,
                    "schema_revision": revision,
                    "counts": counts,
                    "members": members,
                },
                ensure_ascii=False, sort_keys=True,
            ).encode(),
        )
    return buffer.getvalue()


def _encrypt(bundle: bytes, header: dict) -> bytes:
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True).encode()
    key = _derive_key(read_recovery_key(), bytes.fromhex(header["kdf_salt"]))
    ciphertext = AESGCM(key).encrypt(
        bytes.fromhex(header["nonce"]), bundle, associated_data=header_bytes
    )
    return MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + ciphertext


def _read_envelope(path: Path) -> tuple[dict, bytes]:
    """Parse magic + header without decrypting; corrupted structure raises."""
    blob = path.read_bytes()
    if len(blob) < 8 or blob[:4] != MAGIC:
        raise BackupCorrupt(f"{path.name} is not a smart-rag backup")
    (header_len,) = struct.unpack(">I", blob[4:8])
    if len(blob) < 8 + header_len:
        raise BackupCorrupt(f"{path.name} is truncated")
    try:
        header = json.loads(blob[8:8 + header_len])
    except json.JSONDecodeError as failure:
        raise BackupCorrupt(f"{path.name} header is not readable: {failure}") from None
    if int(header.get("format_version", 0)) != FORMAT_VERSION:
        raise BackupCorrupt(
            f"{path.name} has unsupported format version {header.get('format_version')}"
        )
    return header, blob[8 + header_len:]


def _decrypt(header: dict, ciphertext: bytes, recovery_key: str) -> bytes:
    key = _derive_key(recovery_key, bytes.fromhex(header["kdf_salt"]))
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True).encode()
    try:
        bundle = AESGCM(key).decrypt(
            bytes.fromhex(header["nonce"]), ciphertext, associated_data=header_bytes
        )
    except InvalidTag:
        raise BackupKeyError(
            "authentication failed: wrong recovery key or modified backup"
        ) from None
    if hashlib.sha256(bundle).hexdigest() != header["bundle_sha256"]:
        raise BackupCorrupt("bundle checksum mismatch after decryption")
    return bundle


async def create_backup(
    engine: AsyncEngine, store, writer: EventWriter, *, backup_dir: Path, actor: str,
) -> BackupRecord:
    """Dump, encrypt, and register one backup; row + audit commit together."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    tables, revision, source_ids = await _dump(engine)
    counts = {table: len(rows) for table, rows in tables.items()}
    bundle = _bundle(tables, revision, created_at, store)
    header = {
        "format": BUNDLE_FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": created_at,
        "schema_revision": revision,
        "kdf_salt": os.urandom(16).hex(),
        "nonce": os.urandom(12).hex(),
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "counts": counts,
        "source_ids": source_ids,
    }

    backup_id = uuid_module.uuid4()
    filename = f"{backup_id}.srb"
    path = backup_dir / filename
    blob = _encrypt(bundle, header)
    path.write_bytes(blob)
    file_sha256 = sha256_file(path)

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO managed_backups (id, filename, sha256, size_bytes, source_ids)"
                " VALUES (CAST(:id AS uuid), :filename, :sha256, :size, CAST(:sources AS jsonb))"
            ),
            {
                "id": str(backup_id),
                "filename": filename,
                "sha256": file_sha256,
                "size": len(blob),
                "sources": json.dumps(source_ids),
            },
        )
        await writer.record(
            AuditEvent(
                type="backup.created",
                actor=actor,
                subject=str(backup_id),
                metadata={
                    "filename": filename,
                    "sha256": file_sha256,
                    "size_bytes": len(blob),
                    "rows": sum(counts.values()),
                    "sources": len(source_ids),
                },
            ),
            connection,
        )
    return BackupRecord(
        id=backup_id, filename=filename, sha256=file_sha256,
        size_bytes=len(blob), counts=counts,
    )


def inspect_backup(path: Path) -> BackupInfo:
    """Header facts only — no recovery key, no decryption."""
    header, _ = _read_envelope(path)
    return BackupInfo(
        filename=path.name,
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        created_at=str(header["created_at"]),
        format_version=int(header["format_version"]),
        schema_revision=str(header["schema_revision"]),
        counts=dict(header["counts"]),
        source_count=len(header["source_ids"]),
    )


def load_bundle(path: Path, recovery_key: str) -> tuple[dict, bytes]:
    """Authenticate, decrypt, and checksum-verify; returns header + bundle."""
    header, ciphertext = _read_envelope(path)
    return header, _decrypt(header, ciphertext, recovery_key)


class DbManagedBackupInventory:
    """The real Task 5 port: rows in ``managed_backups``, files in backup_dir.

    A backup is purgeable only when its file unlinks; a stuck file leaves the
    row in place so the deletion verification raises ``DeletionIncomplete``
    and the job redelivery reruns the purge (idempotent via ``missing_ok``).
    """

    def __init__(self, engine: AsyncEngine, backup_dir: Path) -> None:
        self._engine = engine
        self._backup_dir = backup_dir

    async def _containing(self, source_id: str) -> list:
        async with self._engine.begin() as connection:
            return [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT id, filename, created_at FROM managed_backups "
                            "WHERE EXISTS ("
                            "  SELECT 1 FROM jsonb_array_elements_text(source_ids) s"
                            "  WHERE s = CAST(:sid AS text)"
                            ") ORDER BY created_at"
                        ),
                        {"sid": source_id},
                    )
                ).mappings()
            ]

    async def list_containing(self, source_id: str) -> list[ManagedBackup]:
        rows = await self._containing(source_id)
        return [
            ManagedBackup(
                id=str(row["id"]),
                path=str(self._backup_dir / str(row["filename"])),
                created_at=row["created_at"],
                source_ids=frozenset({source_id}),
            )
            for row in rows
        ]

    async def purge_containing(self, source_id: str) -> int:
        rows = await self._containing(source_id)
        purged: list[str] = []
        for row in rows:
            try:
                (self._backup_dir / str(row["filename"])).unlink(missing_ok=True)
            except OSError:
                continue  # file stays, row stays: verification blocks the receipt
            purged.append(str(row["id"]))
        if purged:
            async with self._engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM managed_backups WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": purged},
                )
        return len(purged)
