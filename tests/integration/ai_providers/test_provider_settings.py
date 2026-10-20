"""Task 23b: generation provider settings, consent, and audit (live Compose).

Activation is consent-version-checked and test-before-activate; a failed
probe or a mismatched consent leaves no row and no audit. Switching the
generation provider dispatches nothing and touches no index rows — the
embedding path stays Ollama, so no re-index is needed.
"""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.contracts import ProviderUnavailableError
from ai_providers.glm import GLMConfig
from ai_providers.provider_settings import (
    CONSENT_VERSION,
    ConsentVersionMismatch,
    activate_generation_provider,
    get_generation_provider_state,
    revert_generation_provider,
)
from ai_providers.provider_settings import (
    test_provider as probe_provider,
)
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, AccessDenied
from identity_access.setup import create_first_owner

PROVIDER_TABLES = (
    "generation_provider_settings, audit_events, outbox, jobs, sessions, "
    "users, workspaces"
)


def _run(db_settings, coro_factory) -> object:
    async def run() -> object:
        engine = create_async_engine(db_settings.database_url)
        uow = UnitOfWork(engine)
        try:
            return await coro_factory(uow, engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def settings():
    from foundation.config import Settings

    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings):
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {PROVIDER_TABLES} CASCADE"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


@pytest.fixture()
def workspace(db) -> dict:
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(
                uow,
                EventWriter(),
                email="owner@example.com",
                password="owner-password-1",
            )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
        }

    return asyncio.run(create())


def _context(workspace: dict, role: str = "owner") -> AccessContext:
    return AccessContext(
        user_id=workspace["owner_id"],
        workspace_id=workspace["workspace_id"],
        role=role,
    )


def _config(**overrides) -> GLMConfig:
    return GLMConfig(
        base_url="https://glm.example.com/api", model="glm-4.7", **overrides
    )


def _ok_transport(url, payload, headers, timeout):
    assert url.endswith("/chat/completions")
    return 200, json.dumps({"choices": [{"message": {"content": "ready"}}]}).encode()


def _down_transport(url, payload, headers, timeout):
    raise ProviderUnavailableError(f"{url} refused")


def _audit_types(db) -> list[str]:
    async def run(uow, _engine) -> list[str]:
        async with uow.transaction() as transaction:
            rows = (
                (
                    await transaction.execute(
                        text(
                            "SELECT type FROM audit_events WHERE type LIKE 'provider.%' "
                            "ORDER BY created_at"
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [str(row) for row in rows]

    return _run(db, run)


def _counts(db) -> dict:
    async def run(uow, engine) -> dict:
        async with uow.transaction() as transaction:
            outbox = (
                await transaction.execute(text("SELECT count(*) FROM outbox"))
            ).scalar_one()
            generations = (
                await transaction.execute(
                    text("SELECT count(*) FROM index_generations")
                )
            ).scalar_one()
            chunks = (
                await transaction.execute(text("SELECT count(*) FROM index_chunks"))
            ).scalar_one()
        return {"outbox": outbox, "generations": generations, "chunks": chunks}

    return _run(db, run)


def test_default_state_is_ollama(db, workspace):
    state = _run(
        db,
        lambda uow, engine: get_generation_provider_state(
            uow, workspace_id=workspace["workspace_id"]
        ),
    )
    assert state.provider == "ollama"
    assert state.endpoint is None
    assert state.model is None
    assert state.rewrite_enabled is False


def test_activation_persists_state_and_audit(db, workspace):
    state = _run(
        db,
        lambda uow, engine: activate_generation_provider(
            uow,
            EventWriter(),
            context=_context(workspace),
            config=_config(),
            rewrite_enabled=True,
            consent_version=CONSENT_VERSION,
            transport=_ok_transport,
            api_key="test-key",
        ),
    )
    assert state.provider == "glm"
    assert state.endpoint == "https://glm.example.com/api"
    assert state.model == "glm-4.7"
    assert state.rewrite_enabled is True
    assert state.consent_version == CONSENT_VERSION
    assert state.consented_at is not None
    assert _audit_types(db) == ["provider.activated"]


def test_consent_mismatch_refuses_activation(db, workspace):
    with pytest.raises(ConsentVersionMismatch):
        _run(
            db,
            lambda uow, engine: activate_generation_provider(
                uow,
                EventWriter(),
                context=_context(workspace),
                config=_config(),
                rewrite_enabled=False,
                consent_version="1900-01-01",
                transport=_ok_transport,
                api_key="test-key",
            ),
        )
    assert _audit_types(db) == []
    state = _run(
        db,
        lambda uow, engine: get_generation_provider_state(
            uow, workspace_id=workspace["workspace_id"]
        ),
    )
    assert state.provider == "ollama"


def test_failed_probe_refuses_activation(db, workspace):
    with pytest.raises(ProviderUnavailableError):
        _run(
            db,
            lambda uow, engine: activate_generation_provider(
                uow,
                EventWriter(),
                context=_context(workspace),
                config=_config(),
                rewrite_enabled=False,
                consent_version=CONSENT_VERSION,
                transport=_down_transport,
                api_key="test-key",
            ),
        )
    assert _audit_types(db) == []
    state = _run(
        db,
        lambda uow, engine: get_generation_provider_state(
            uow, workspace_id=workspace["workspace_id"]
        ),
    )
    assert state.provider == "ollama"


def test_activation_never_touches_index_or_outbox(db, workspace):
    _run(
        db,
        lambda uow, engine: activate_generation_provider(
            uow,
            EventWriter(),
            context=_context(workspace),
            config=_config(),
            rewrite_enabled=True,
            consent_version=CONSENT_VERSION,
            transport=_ok_transport,
            api_key="test-key",
        ),
    )
    counts = _counts(db)
    assert counts == {"outbox": 0, "generations": 0, "chunks": 0}


def test_revert_restores_default_and_audits(db, workspace):
    _run(
        db,
        lambda uow, engine: activate_generation_provider(
            uow,
            EventWriter(),
            context=_context(workspace),
            config=_config(),
            rewrite_enabled=False,
            consent_version=CONSENT_VERSION,
            transport=_ok_transport,
            api_key="test-key",
        ),
    )
    state = _run(
        db,
        lambda uow, engine: revert_generation_provider(
            uow, EventWriter(), context=_context(workspace)
        ),
    )
    assert state.provider == "ollama"
    assert state.endpoint is None
    assert _audit_types(db) == ["provider.activated", "provider.reverted"]


def test_non_owner_refused(db, workspace):
    for role in ("admin", "member"):
        with pytest.raises(AccessDenied):
            _run(
                db,
                lambda uow, engine, role=role: activate_generation_provider(
                    uow,
                    EventWriter(),
                    context=_context(workspace, role),
                    config=_config(),
                    rewrite_enabled=False,
                    consent_version=CONSENT_VERSION,
                    transport=_ok_transport,
                    api_key="test-key",
                ),
            )
        with pytest.raises(AccessDenied):
            _run(
                db,
                lambda uow, engine, role=role: revert_generation_provider(
                    uow, EventWriter(), context=_context(workspace, role)
                ),
            )
    assert _audit_types(db) == []


def test_probe_sends_content_free_prompt(db, workspace):
    captured: dict = {}

    def spy_transport(url, payload, headers, timeout):
        captured["payload"] = payload
        return _ok_transport(url, payload, headers, timeout)

    result = _run(
        db,
        lambda uow, engine: probe_provider(
            _context(workspace),
            _config(),
            transport=spy_transport,
            api_key="test-key",
        ),
    )
    assert result.text == "ready"
    messages = captured["payload"]["messages"]
    assert [m["role"] for m in messages] == ["user"]
    assert captured["payload"]["model"] == "glm-4.7"
    assert "example" not in json.dumps(messages)  # no workspace content
