"""Task 6e: host-local owner password recovery.

The recovery script is importable from ``infra/scripts/`` and its guards are
tested directly: password inputs are refused by design, only the owning OS
account may run it, the reset forces a password change, revokes sessions,
records one audit event, and preserves all data.
"""

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.auth import AuthenticationFailed, authenticate, require_session
from identity_access.setup import create_first_owner

ROOT = Path(__file__).resolve().parents[3]
OWNER_EMAIL = "owner@example.com"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "reset_owner_password", ROOT / "infra" / "scripts" / "reset_owner_password.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


@pytest.fixture()
def workspace(db: Settings) -> str:
    async def create() -> str:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(
                uow, EventWriter(), email=OWNER_EMAIL, password="owner-password-1"
            )
        finally:
            await engine.dispose()
        return owner.user_id

    return asyncio.run(create())


def test_password_argument_is_refused(script) -> None:
    with pytest.raises(script.RecoveryRefused) as excinfo:
        script.refuse_password_inputs(["prog", "--password", "hunter2"], {})
    assert "command line" in str(excinfo.value)

    with pytest.raises(script.RecoveryRefused):
        script.refuse_password_inputs([], {"SMART_RAG_NEW_PASSWORD": "hunter2"})


def test_email_argument_is_accepted(script) -> None:
    script.refuse_password_inputs(["prog", "--email", "owner@example.com"], {})


def test_recovery_requires_the_owning_os_account(script, tmp_path: Path) -> None:
    with pytest.raises(script.RecoveryRefused) as excinfo:
        script.require_host_owner(str(tmp_path), euid=os.geteuid() + 1)
    assert "another OS account" in str(excinfo.value)

    with pytest.raises(script.RecoveryRefused) as missing:
        script.require_host_owner(str(tmp_path / "absent"), euid=os.geteuid())
    assert "make up" in str(missing.value)

    script.require_host_owner(str(tmp_path), euid=os.geteuid())  # passes


def test_prompt_rejects_mismatch_and_short_password(script, monkeypatch) -> None:
    entries = iter(["short", "short"])
    monkeypatch.setattr(
        script.getpass, "getpass", lambda _prompt: next(entries)
    )
    with pytest.raises(script.RecoveryRefused):
        script.prompt_new_password()

    entries = iter(["first-password", "second-password"])
    monkeypatch.setattr(
        script.getpass, "getpass", lambda _prompt: next(entries)
    )
    with pytest.raises(script.RecoveryRefused):
        script.prompt_new_password()


def test_find_owner_single_and_multiple(script, db, workspace) -> None:
    settings = db
    user_id, email = script.find_owner(settings, None)
    assert email == OWNER_EMAIL
    assert user_id == workspace

    async def add_second_owner() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO users (id, workspace_id, email, password_hash, role) "
                        "SELECT gen_random_uuid(), workspace_id, 'owner2@example.com', "
                        "'x', 'owner' FROM users WHERE role = 'owner' LIMIT 1"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(add_second_owner())
    with pytest.raises(script.RecoveryRefused) as excinfo:
        script.find_owner(settings, None)
    assert "--email" in str(excinfo.value)

    _, found_email = script.find_owner(settings, "owner2@example.com")
    assert found_email == "owner2@example.com"


def test_full_recovery_forces_change_revokes_sessions_and_audits(
    script, db, workspace
) -> None:
    settings = db
    user_id, _ = script.find_owner(settings, None)

    async def sign_in_first() -> str:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        try:
            session = await authenticate(uow, EventWriter(), OWNER_EMAIL, "owner-password-1")
            return session.token
        finally:
            await engine.dispose()

    stale_token = asyncio.run(sign_in_first())

    async def apply() -> None:
        await script._apply(settings, user_id, "recovered-pass-1", actor="host:simon")

    asyncio.run(apply())

    async def verify() -> tuple[int, int, bool, bool]:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        try:
            async with engine.connect() as connection:
                users = (
                    await connection.execute(text("SELECT count(*) FROM users"))
                ).scalar_one()
                audits = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM audit_events WHERE type = 'user.password_reset'"
                        )
                    )
                ).scalar_one()
            old_rejected = False
            try:
                await authenticate(uow, EventWriter(), OWNER_EMAIL, "owner-password-1")
            except AuthenticationFailed:
                old_rejected = True
            new_session = await authenticate(
                uow, EventWriter(), OWNER_EMAIL, "recovered-pass-1"
            )
            new_user = await require_session(uow, new_session.token)
        finally:
            await engine.dispose()
        return users, audits, old_rejected, new_user.must_change_password

    users, audits, old_rejected, must_change = asyncio.run(verify())
    assert (users, audits) == (1, 1)
    assert old_rejected is True
    assert must_change is True

    async def stale_is_dead() -> bool:
        engine = create_async_engine(settings.database_url)
        uow = UnitOfWork(engine)
        try:
            from identity_access.auth import SessionInvalid

            try:
                await require_session(uow, stale_token)
            except SessionInvalid:
                return True
        finally:
            await engine.dispose()
        return False

    assert asyncio.run(stale_is_dead()) is True


def test_reset_unknown_user_raises(script, db) -> None:
    async def run() -> None:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            from identity_access.invitations import reset_user_password

            await reset_user_password(
                uow,
                EventWriter(),
                user_id="00000000-0000-0000-0000-000000000000",
                new_password="whatever-pass-1",
                actor="host:test",
            )
        finally:
            await engine.dispose()

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_recovery_never_writes_password_to_audit(script, db, workspace) -> None:
    settings = db
    user_id, _ = script.find_owner(settings, None)
    secret = "recovered-pass-2"

    asyncio.run(script._apply(settings, user_id, secret, actor="host:simon"))

    async def audit_payloads() -> list[str]:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT actor, subject, metadata::text FROM audit_events "
                            "WHERE type = 'user.password_reset'"
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
        return [str(cell) for row in rows for cell in row]

    for cell in asyncio.run(audit_payloads()):
        assert secret not in cell
