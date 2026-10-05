"""First-run workspace initialization (Task 6a) and dependency checks (6c).

``create_first_owner`` creates the workspace and its first owner atomically
and exactly once: the first-owner exclusivity check, both inserts, and the
``workspace.initialized`` audit event share one transaction, so a failure
anywhere leaves the database untouched. ``check_setup_dependencies`` and
``initialize_workspace`` give the first-run wizard an actionable, resumable
gate: the wizard re-runs after the operator fixes a dependency.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from foundation.config import Settings
from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.passwords import hash_password


class WorkspaceAlreadyInitialized(Exception):
    """A first owner already exists; only host-local recovery may proceed."""


@dataclass(frozen=True)
class CreatedOwner:
    workspace_id: str
    user_id: str


async def create_first_owner(
    uow: UnitOfWork,
    writer: EventWriter,
    email: str,
    password: str,
    workspace_name: str = "Workspace",
) -> CreatedOwner:
    async with uow.transaction() as transaction:
        existing = await transaction.execute(text("SELECT 1 FROM users LIMIT 1"))
        if existing.first() is not None:
            raise WorkspaceAlreadyInitialized(
                "workspace already has a user; use 'make reset-owner-password' to recover"
            )
        workspace_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        await transaction.execute(
            text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
            {"id": workspace_id, "name": workspace_name},
        )
        await transaction.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, role, status) "
                "VALUES (:id, :workspace_id, :email, :password_hash, 'owner', 'active')"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": email,
                "password_hash": hash_password(password),
            },
        )
        await writer.record(
            AuditEvent(type="workspace.initialized", actor=email, subject=workspace_id),
            transaction,
        )
    return CreatedOwner(workspace_id=workspace_id, user_id=user_id)


# --- first-run dependency checks (Task 6c) ------------------------------------

Probe = Callable[[Settings], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    ok: bool
    detail: str
    remediation: str = ""


@dataclass(frozen=True)
class SetupReadiness:
    checks: list[DependencyCheck]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def failures(self) -> list[DependencyCheck]:
        return [check for check in self.checks if not check.ok]


class SetupDependenciesFailed(Exception):
    """Carries the failing checks so callers render actionable remediation."""

    def __init__(self, readiness: SetupReadiness) -> None:
        super().__init__("setup dependencies failed: " + ", ".join(c.name for c in readiness.failures()))
        self.readiness = readiness


async def check_setup_dependencies(
    settings: Settings,
    *,
    probe_database: Probe | None = None,
    probe_redis: Probe | None = None,
    probe_storage: Probe | None = None,
    probe_ollama: Probe | None = None,
) -> SetupReadiness:
    """Verify database, Redis, storage, and Ollama; probes are injectable."""
    database: tuple[bool, str] = await (probe_database or _probe_database)(settings)
    redis: tuple[bool, str] = await (probe_redis or _probe_redis)(settings)
    storage: tuple[bool, str] = await (probe_storage or _probe_storage)(settings)
    ollama: tuple[bool, str] = await (probe_ollama or _probe_ollama)(settings)
    profile = settings.active_profile
    checks = [
        DependencyCheck(
            "database", *database, remediation="run: make up (starts PostgreSQL)"
        ),
        DependencyCheck("redis", *redis, remediation="run: make up (starts Redis)"),
        DependencyCheck(
            "storage",
            *storage,
            remediation="grant write access to SMART_RAG_DATA_DIR "
            f"({settings.data_dir})",
        ),
        DependencyCheck(
            "ollama",
            *ollama,
            remediation=f"pull the models: ollama pull {profile.chat_model} && "
            f"ollama pull {profile.embedding_model}",
        ),
    ]
    return SetupReadiness(checks=checks)


async def initialize_workspace(
    uow: UnitOfWork,
    writer: EventWriter,
    settings: Settings,
    email: str,
    password: str,
    *,
    workspace_name: str = "Workspace",
    readiness: SetupReadiness | None = None,
) -> CreatedOwner:
    """Gate on dependencies (resumable: fix and rerun), then create the owner."""
    if readiness is not None and not readiness.ok:
        raise SetupDependenciesFailed(readiness)
    return await create_first_owner(
        uow, writer, email=email, password=password, workspace_name=workspace_name
    )


async def _probe_database(settings: Settings) -> tuple[bool, str]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as error:  # noqa: BLE001 - setup must report, not crash
        return False, type(error).__name__
    finally:
        await engine.dispose()
    return True, "reachable"


async def _probe_redis(settings: Settings) -> tuple[bool, str]:
    from redis.asyncio import Redis

    client = Redis.from_url(settings.redis_url, socket_connect_timeout=3.0)
    try:
        await client.ping()
    except Exception as error:  # noqa: BLE001
        return False, type(error).__name__
    finally:
        await client.aclose()
    return True, "reachable"


async def _probe_storage(settings: Settings) -> tuple[bool, str]:
    try:
        root = Path(settings.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, suffix=".probe", delete=True) as handle:
            handle.write(b"probe")
    except OSError as error:
        return False, f"unwritable: {error.strerror or type(error).__name__}"
    return True, "writable"


async def _probe_ollama(settings: Settings) -> tuple[bool, str]:
    import json
    import urllib.request

    def fetch() -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(
                f"{settings.ollama_host}/api/tags", timeout=3.0
            ) as response:
                models = [m["name"] for m in json.load(response).get("models", [])]
        except Exception as error:  # noqa: BLE001
            return False, type(error).__name__
        profile = settings.active_profile
        missing = [
            wanted
            for wanted in (profile.chat_model, profile.embedding_model)
            if not any(available.startswith(wanted) for available in models)
        ]
        if missing:
            return False, f"model(s) missing: {', '.join(missing)}"
        return True, f"{profile.chat_model} + {profile.embedding_model} available"

    return await asyncio.to_thread(fetch)
