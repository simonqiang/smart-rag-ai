"""Host-local encrypted backup CLI (Task 24b).

create   dump and encrypt one managed backup into the configured backup dir
inspect  show a backup's header facts (no recovery key needed)
restore  rebuild a clean installation from a backup file

The recovery key arrives through SMART_RAG_BACKUP_KEY at call time — like
the GLM key it is never stored, printed, or logged. Restore refuses a dirty
target or a foreign schema revision; run it only against a fresh install
(host script only — a restore into the live system can never be clean).

Usage:
    python infra/scripts/backup_restore.py create
    python infra/scripts/backup_restore.py inspect <file>
    python infra/scripts/backup_restore.py restore <file>
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from operations import backup


def _run(command: str, path: Path | None) -> str:
    settings = Settings.load()
    actor = f"host:{getpass.getuser()}"

    if command == "create":
        async def create():
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy.pool import NullPool

            engine = create_async_engine(settings.database_url, poolclass=NullPool)
            try:
                return await backup.create_backup(
                    engine, ObjectStore(Path(settings.data_dir)), EventWriter(),
                    backup_dir=Path(settings.backup_dir), actor=actor,
                )
            finally:
                await engine.dispose()

        record = asyncio.run(create())
        return (
            f"created {record.filename} ({record.size_bytes} bytes, "
            f"{sum(record.counts.values())} rows); keep the recovery key safe — "
            "backups are useless without it"
        )

    if command == "inspect":
        info = backup.inspect_backup(path)
        counts = ", ".join(f"{table}={n}" for table, n in sorted(info.counts.items()) if n)
        return (
            f"{info.filename}: created {info.created_at}, schema {info.schema_revision}, "
            f"sha256 {info.sha256}, {info.size_bytes} bytes, {info.source_count} sources"
            + (f"; {counts}" if counts else "")
        )

    recovery_key = backup.read_recovery_key()

    async def restore():
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            return await backup.restore_backup(
                engine, ObjectStore(Path(settings.data_dir)), EventWriter(),
                path=path, recovery_key=recovery_key, actor=actor,
            )
        finally:
            await engine.dispose()

    record = asyncio.run(restore())
    return (
        f"restored {record.filename}: {record.rows} rows, {record.sources} sources. "
        "All previous sessions are gone; sign in again."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="manage encrypted smart-rag backups")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="dump and encrypt one managed backup")
    inspect = sub.add_parser("inspect", help="show a backup's header facts")
    inspect.add_argument("file", type=Path)
    restore = sub.add_parser("restore", help="rebuild a clean installation")
    restore.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        print(_run(args.command, getattr(args, "file", None)))
    except (
        backup.BackupKeyError,
        backup.BackupCorrupt,
        backup.BackupIncompatible,
        backup.TargetNotEmpty,
    ) as failure:
        print(f"backup failed: {failure}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
