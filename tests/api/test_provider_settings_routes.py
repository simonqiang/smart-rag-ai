"""Task 23c: provider settings routes — state visibility, owner-only changes,
actionable probe failures, and the Ask route's dynamic provider wiring."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.contracts import ProviderUnavailableError
from ai_providers.fakes import FakeEmbeddingProvider
from ai_providers.glm import GLM_REWRITE_POLICY
from ai_providers.provider_settings import CONSENT_VERSION
from apps.api import main as api
from apps.api.routes import ask as ask_routes
from apps.api.routes import provider_settings as provider_routes
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.setup import create_first_owner

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}
ADMIN = {"email": "admin@example.com", "password": "admin-password-1"}
MEMBER = {"email": "member@example.com", "password": "member-password-1"}
ENDPOINT = "https://glm.example/v1"
MODEL = "glm-4"

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

TABLES = (
    "generation_provider_settings, index_chunks, index_generations, source_versions, "
    "sources, collections, conversations, retention_policies, invitations, "
    "sessions, users, workspaces, jobs, outbox"
)


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def workspace():
    async def create() -> dict:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), **OWNER)
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id, "owner_id": owner.user_id}

    return asyncio.run(create())


@pytest.fixture()
def client():
    return TestClient(api.app)


def _sign_in(client: TestClient, person: dict) -> None:
    response = client.post("/api/session", json=person)
    assert response.status_code == 200, response.text


def _invite_and_accept(client: TestClient, email: str, password: str, role: str) -> None:
    _sign_in(client, OWNER)
    invitation = client.post(
        "/api/users/invitations",
        json={"email": email, "role": role, "collections": []},
    )
    assert invitation.status_code == 201, invitation.text
    client.post("/api/session/logout")
    accepted = client.post(
        "/api/invitations/accept",
        json={"token": invitation.json()["token"], "password": password},
    )
    assert accepted.status_code in (200, 201), accepted.text
    client.post("/api/session/logout")


def _completion(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _transport_script(status: int = 200, body: bytes | None = None,
                      error: Exception | None = None):
    """Capturing fake transport standing in for the GLM endpoint."""
    calls: list[dict] = []

    def transport(url, payload, headers, timeout):
        calls.append(payload)
        if error is not None:
            raise error
        return status, b"" if body is None else body

    return transport, calls


def _activate(client: TestClient, *, rewrite_enabled: bool):
    return client.post(
        "/api/settings/ai-provider/activate",
        json={
            "endpoint": ENDPOINT, "model": MODEL,
            "rewrite_enabled": rewrite_enabled, "consent_version": CONSENT_VERSION,
        },
    )


DEFAULT_STATE = {
    "provider": "ollama", "endpoint": None, "model": None,
    "rewrite_enabled": False, "consent_version": None, "consented_at": None,
}


def test_state_is_visible_to_signed_in_users(client, workspace) -> None:
    _sign_in(client, OWNER)
    assert client.get("/api/settings/ai-provider").json() == DEFAULT_STATE


def test_owner_probes_and_activates_glm(client, workspace, monkeypatch) -> None:
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "probe-key")
    transport, calls = _transport_script(body=_completion("ready"))
    provider_routes._transport = transport
    try:
        _sign_in(client, OWNER)
        probe = client.post(
            "/api/settings/ai-provider/test",
            json={"endpoint": ENDPOINT, "model": MODEL},
        )
        assert probe.status_code == 200, probe.text
        assert probe.json() == {"ok": True}
        # Testing persists nothing — the provider is still the default.
        assert client.get("/api/settings/ai-provider").json() == DEFAULT_STATE

        activated = _activate(client, rewrite_enabled=True)
        assert activated.status_code == 200, activated.text
        body = activated.json()
        assert body["provider"] == "glm"
        assert body["endpoint"] == ENDPOINT
        assert body["model"] == MODEL
        assert body["rewrite_enabled"] is True
        assert body["consent_version"] == CONSENT_VERSION
        assert body["consented_at"]
        assert client.get("/api/settings/ai-provider").json()["provider"] == "glm"
    finally:
        provider_routes._transport = None
    # Both round-trips sent only the fixed content-free probe prompt.
    assert [call["messages"][-1]["content"] for call in calls] == [
        "Reply with the single word: ready", "Reply with the single word: ready",
    ]


def test_ask_badge_flips_and_rewriter_wires_only_when_enabled(
    client, workspace, monkeypatch
) -> None:
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "ask-key")
    transport, calls = _transport_script(body=_completion("rewritten question"))
    provider_routes._transport = transport
    ask_routes._embeddings = FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions)
    ask_routes._profile = TEST_PROFILE
    ask_routes._glm_transport = transport
    try:
        _sign_in(client, OWNER)
        assert _activate(client, rewrite_enabled=True).status_code == 200
        calls.clear()  # drop the activation probe from the capture

        answer = client.post("/api/ask", json={"question": "How do I onboard?"})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["provider"] == {"name": "glm", "local": False}
        # Empty index: retrieval finds nothing, generation is bypassed, so the
        # only GLM call was the rewrite.
        assert body["insufficient_evidence"] is True
        assert len(calls) == 1
        assert calls[0]["messages"][0]["content"] == GLM_REWRITE_POLICY

        # With rewriting disabled the ask path sends nothing to GLM.
        assert _activate(client, rewrite_enabled=False).status_code == 200
        calls.clear()
        answer = client.post("/api/ask", json={"question": "And now?"})
        assert answer.status_code == 200, answer.text
        assert answer.json()["provider"] == {"name": "glm", "local": False}
        assert calls == []
    finally:
        provider_routes._transport = None
        ask_routes._embeddings = None
        ask_routes._profile = None
        ask_routes._glm_transport = None


def test_activation_refuses_a_stale_consent_version(client, workspace) -> None:
    transport, calls = _transport_script(body=_completion("ready"))
    provider_routes._transport = transport
    try:
        _sign_in(client, OWNER)
        refused = client.post(
            "/api/settings/ai-provider/activate",
            json={"endpoint": ENDPOINT, "model": MODEL, "consent_version": "1900-01-01"},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason"] == "consent_required"
        assert client.get("/api/settings/ai-provider").json() == DEFAULT_STATE
    finally:
        provider_routes._transport = None
    assert calls == []  # consent is checked before any network round-trip


def test_a_rejected_key_maps_to_an_actionable_400(client, workspace, monkeypatch) -> None:
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "wrong-key")
    transport, _ = _transport_script(status=401)
    provider_routes._transport = transport
    try:
        _sign_in(client, OWNER)
        probe = client.post(
            "/api/settings/ai-provider/test",
            json={"endpoint": ENDPOINT, "model": MODEL},
        )
        assert probe.status_code == 400, probe.text
        assert probe.json()["detail"]["reason"] == "invalid_key"

        refused = _activate(client, rewrite_enabled=False)
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"]["reason"] == "invalid_key"
        # A failed probe leaves nothing behind.
        assert client.get("/api/settings/ai-provider").json() == DEFAULT_STATE
    finally:
        provider_routes._transport = None


def test_a_missing_env_key_and_an_outage_map_to_actionable_payloads(
    client, workspace, monkeypatch
) -> None:
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "key")
    _sign_in(client, OWNER)
    try:
        monkeypatch.delenv("SMART_RAG_GLM_API_KEY", raising=False)
        provider_routes._transport = _transport_script()[0]
        missing = client.post(
            "/api/settings/ai-provider/test",
            json={"endpoint": ENDPOINT, "model": MODEL},
        )
        assert missing.status_code == 400, missing.text
        assert missing.json()["detail"]["reason"] == "missing_key"
    finally:
        provider_routes._transport = None
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "key")  # delenv lasts to teardown

    transport, _ = _transport_script(error=ProviderUnavailableError("down"))
    provider_routes._transport = transport
    try:
        outage = client.post(
            "/api/settings/ai-provider/test",
            json={"endpoint": ENDPOINT, "model": MODEL},
        )
        assert outage.status_code == 503, outage.text
        assert outage.json()["detail"]["reason"] == "unreachable"

        transport, _ = _transport_script(status=200, body=b"not json")
        provider_routes._transport = transport
        malformed = client.post(
            "/api/settings/ai-provider/test",
            json={"endpoint": ENDPOINT, "model": MODEL},
        )
        assert malformed.status_code == 503, malformed.text
        assert malformed.json()["detail"]["reason"] == "invalid_response"
        assert "check the endpoint URL" in malformed.json()["detail"]["message"]
    finally:
        provider_routes._transport = None


def test_owner_reverts_to_the_ollama_default(client, workspace, monkeypatch) -> None:
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "key")
    provider_routes._transport = _transport_script(body=_completion("ready"))[0]
    try:
        _sign_in(client, OWNER)
        assert _activate(client, rewrite_enabled=False).status_code == 200
        reverted = client.post("/api/settings/ai-provider/revert")
        assert reverted.status_code == 200, reverted.text
        assert reverted.json() == DEFAULT_STATE
        assert client.get("/api/settings/ai-provider").json() == DEFAULT_STATE
    finally:
        provider_routes._transport = None


def test_only_owners_may_modify(client, workspace) -> None:
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")
    _invite_and_accept(client, MEMBER["email"], MEMBER["password"], "member")
    for person in (ADMIN, MEMBER):
        _sign_in(client, person)
        base = "/api/settings/ai-provider"
        assert client.get(base).status_code == 200
        assert client.post(
            f"{base}/test", json={"endpoint": ENDPOINT, "model": MODEL},
        ).status_code == 403
        assert client.post(
            f"{base}/activate",
            json={"endpoint": ENDPOINT, "model": MODEL, "consent_version": CONSENT_VERSION},
        ).status_code == 403
        assert client.post(f"{base}/revert").status_code == 403
