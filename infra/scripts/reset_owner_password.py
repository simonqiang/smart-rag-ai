"""Host-local owner password recovery (Task 6e).

Run on the machine that owns the workspace data: ``make reset-owner-password``.
The new password is always typed interactively — never accepted as an argument
or environment variable, never logged. The reset sets a temporary password
(forcing a change at next sign-in), revokes all sessions, records the audit
event transactionally, and touches nothing else.

Usage: python infra/scripts/reset_owner_password.py [--email owner@example.com]
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.invitations import reset_user_password

MIN_PASSWORD_LENGTH = 10


class RecoveryRefused(Exception):
    """Actionable refusal; message tells the operator what to do."""


def refuse_password_inputs(argv: list[str], env: dict[str, str]) -> None:
    """Passwords arrive by prompt only: never argv, never the environment."""
    for arg in argv[1:]:
        if "password" in arg.lower() or arg in ("-p", "-P"):
            raise RecoveryRefused(
                "refusing password on the command line; run "
                "'make reset-owner-password' and type it when prompted"
            )
        if arg != "--email" and not _is_email_value(argv, arg):
            raise RecoveryRefused(f"unsupported argument: {arg!r}")
    for name in env:
        if name.startswith("SMART_RAG_") and "PASSWORD" in name:
            raise RecoveryRefused(
                f"refusing {name} from the environment; unset it and type the "
                "password when prompted"
            )


def _is_email_value(argv: list[str], arg: str) -> bool:
    for index, candidate in enumerate(argv):
        if candidate == "--email" and index + 1 < len(argv) and argv[index + 1] == arg:
            return True
    return False


def require_host_owner(data_dir: str, euid: int | None = None) -> None:
    """Only the OS account that owns the data directory may recover access."""
    directory = Path(data_dir)
    if not directory.is_dir():
        raise RecoveryRefused(
            f"data directory {data_dir} does not exist; run 'make up' first"
        )
    uid = os.geteuid() if euid is None else euid
    if directory.stat().st_uid != uid:
        raise RecoveryRefused(
            f"data directory {data_dir} is owned by another OS account; "
            "recovery must run on the host as that account"
        )


def find_owner(settings: Settings, email: str | None) -> tuple[str, str]:
    """Return (user_id, email) of the owner to recover."""

    async def run() -> tuple[str, str]:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                if email is None:
                    rows = (
                        (
                            await connection.execute(
                                text("SELECT id, email FROM users WHERE role = 'owner'")
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if not rows:
                        raise RecoveryRefused(
                            "no owner exists yet; initialize the workspace "
                            "through the setup wizard"
                        )
                    if len(rows) > 1:
                        raise RecoveryRefused(
                            "several owners exist; pass --email <address>"
                        )
                    return str(rows[0]["id"]), str(rows[0]["email"])
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT id, email FROM users "
                                "WHERE role = 'owner' AND email = :email"
                            ),
                            {"email": email},
                        )
                    )
                    .mappings()
                    .first()
                )
        finally:
            await engine.dispose()
        if row is None:
            raise RecoveryRefused(f"no owner with email {email!r}")
        return str(row["id"]), str(row["email"])

    return asyncio.run(run())


def prompt_new_password() -> str:
    first = getpass.getpass("new owner password: ")
    second = getpass.getpass("repeat new owner password: ")
    if first != second:
        raise RecoveryRefused("the two entries do not match; nothing was changed")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise RecoveryRefused(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters; "
            "nothing was changed"
        )
    return first


async def _apply(settings: Settings, user_id: str, temp_password: str, actor: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    uow = UnitOfWork(engine)
    try:
        await reset_user_password(
            uow, EventWriter(), user_id=user_id, new_password=temp_password, actor=actor
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    settings = Settings.load()
    try:
        refuse_password_inputs(argv, dict(os.environ))
        require_host_owner(settings.data_dir)
        email = _email_argument(argv)
        user_id, owner_email = find_owner(settings, email)
        temp_password = prompt_new_password()
        asyncio.run(_apply(settings, user_id, temp_password, actor=f"host:{getpass.getuser()}"))
    except RecoveryRefused as refusal:
        print(f"recovery refused: {refusal}", file=sys.stderr)
        return 2
    print(
        f"temporary password set for {owner_email}; all sessions were revoked. "
        "Sign in on the host and change the password when prompted."
    )
    return 0


def _email_argument(argv: list[str]) -> str | None:
    if "--email" not in argv:
        return None
    index = argv.index("--email")
    if index + 1 >= len(argv):
        raise RecoveryRefused("--email needs a value")
    return argv[index + 1]


if __name__ == "__main__":
    raise SystemExit(main())
