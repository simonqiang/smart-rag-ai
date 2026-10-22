"""Safe upgrade, schema compatibility guard, graceful job drain, and rollback CLI (Task 28).

Guarantees:
1. Unsafe schema downgrades are refused before any service or migration runs.
2. Graceful shutdown drains in-progress (`running`) jobs back to `pending` (recoverable state).
3. Every upgrade creates an encrypted pre-upgrade backup before applying migrations.
4. If migration fails, the error reports the exact pre-upgrade backup and rollback command.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from foundation.config import Settings
from foundation.events import AuditEvent, EventWriter
from foundation.storage import ObjectStore
from operations import backup

KNOWN_REVISIONS: tuple[str, ...] = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
    "0012",
    "0013",
    "0014",
    "0015",
)
EXPECTED_HEAD = KNOWN_REVISIONS[-1]


class UnsafeDowngradeRefused(Exception):
    """Database schema is newer than this application revision supports."""


class UpgradeFailed(Exception):
    """Migration failed; carries the pre-upgrade backup filename for rollback."""

    def __init__(self, message: str, *, backup_filename: str) -> None:
        super().__init__(message)
        self.backup_filename = backup_filename


@dataclass(frozen=True)
class SchemaCompatibility:
    current_revision: str | None
    expected_head: str
    compatible: bool
    needs_migration: bool


@dataclass(frozen=True)
class UpgradeResult:
    status: str
    previous_revision: str | None
    target_revision: str
    backup_filename: str
    drained_jobs: int


def _revision_index(rev: str) -> int:
    if rev not in KNOWN_REVISIONS:
        return -1
    return KNOWN_REVISIONS.index(rev)


async def check_schema_compatibility(
    settings: Settings,
    *,
    expected_head: str = EXPECTED_HEAD,
    current_override: str | None = None,
) -> SchemaCompatibility:
    """Verify database revision is <= expected_head; refuse unsafe downgrades."""
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    current = current_override
    if current is None:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                try:
                    current = (
                        await conn.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalar_one_or_none()
                except ProgrammingError:
                    current = None
        finally:
            await engine.dispose()

    if current is None:
        return SchemaCompatibility(
            current_revision=None,
            expected_head=expected_head,
            compatible=True,
            needs_migration=True,
        )

    cur_idx = _revision_index(current)
    exp_idx = _revision_index(expected_head)
    if cur_idx == -1 or exp_idx == -1 or cur_idx > exp_idx:
        raise UnsafeDowngradeRefused(
            f"unsafe downgrade refused: database is at revision {current!r}, "
            f"which is newer than or incompatible with supported head {expected_head!r}. "
            "Restore the matching pre-upgrade backup into a clean installation instead."
        )

    return SchemaCompatibility(
        current_revision=current,
        expected_head=expected_head,
        compatible=True,
        needs_migration=cur_idx < exp_idx,
    )


async def drain_in_progress_jobs(settings: Settings, *, actor: str) -> int:
    """Return any in-progress (`running`) jobs to `pending` so they recover after restart."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    writer = EventWriter()
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "UPDATE jobs SET status = 'pending', lease_owner = NULL, "
                        "lease_expires_at = NULL, updated_at = now() "
                        "WHERE status = 'running' RETURNING id"
                    )
                )
            ).all()
            if rows:
                await writer.record(
                    AuditEvent(
                        type="operations.jobs_drained",
                        actor=actor,
                        subject="jobs",
                        metadata={"recovered_count": len(rows)},
                    ),
                    conn,
                )
            return len(rows)
    finally:
        await engine.dispose()


def _default_migrate(_settings: Settings, target_revision: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", target_revision],
        cwd=str(ROOT),
        check=True,
    )


async def run_upgrade(
    settings: Settings,
    *,
    target_revision: str = EXPECTED_HEAD,
    migrate_fn: Callable[[Settings, str], None] = _default_migrate,
    actor: str = "host:operator",
) -> UpgradeResult:
    """Perform a safe upgrade with downgrade guard, job drain, and pre-upgrade backup."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    compat = await check_schema_compatibility(settings, expected_head=target_revision)
    drained = await drain_in_progress_jobs(settings, actor=actor)

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        record = await backup.create_backup(
            engine,
            ObjectStore(Path(settings.data_dir)),
            EventWriter(),
            backup_dir=Path(settings.backup_dir),
            actor=actor,
        )
    finally:
        await engine.dispose()

    try:
        migrate_fn(settings, target_revision)
    except Exception as exc:
        backup_path = Path(settings.backup_dir) / record.filename
        raise UpgradeFailed(
            f"upgrade to {target_revision} failed ({exc}); rollback with: "
            f"make restore FILE={backup_path}",
            backup_filename=record.filename,
        ) from exc

    return UpgradeResult(
        status="upgraded",
        previous_revision=compat.current_revision,
        target_revision=target_revision,
        backup_filename=record.filename,
        drained_jobs=drained,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smart RAG AI safe upgrade and schema check")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify database schema compatibility (refuses downgrades)")
    sub.add_parser("drain", help="gracefully return running jobs to pending")
    up_p = sub.add_parser("run", help="create pre-upgrade backup, drain jobs, and migrate")
    up_p.add_argument("--target", default=EXPECTED_HEAD)

    args = parser.parse_args(argv)
    settings = Settings.load()
    actor = f"host:{getpass.getuser()}"
    try:
        if args.command == "check":
            state = asyncio.run(check_schema_compatibility(settings))
            print(
                f"schema compatible: current={state.current_revision} "
                f"expected={state.expected_head} needs_migration={state.needs_migration}"
            )
        elif args.command == "drain":
            count = asyncio.run(drain_in_progress_jobs(settings, actor=actor))
            print(f"drained {count} in-progress job(s)")
        elif args.command == "run":
            res = asyncio.run(
                run_upgrade(settings, target_revision=args.target, actor=actor)
            )
            print(
                f"upgraded {res.previous_revision} -> {res.target_revision} "
                f"(pre-upgrade backup: {res.backup_filename}, drained: {res.drained_jobs})"
            )
    except (UnsafeDowngradeRefused, UpgradeFailed, backup.BackupKeyError) as err:
        print(f"upgrade error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
