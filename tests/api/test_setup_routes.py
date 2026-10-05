"""Task 6c: setup and session API routes against the live Compose stack."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api import main as api
from apps.api.routes import auth as auth_routes
from foundation.config import Settings
from identity_access.setup import DependencyCheck, SetupReadiness

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def _clean():
    async def reset() -> None:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE invitations, sessions, users, workspaces CASCADE")
            )
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())


def test_setup_status_reports_uninitialized_with_dependencies() -> None:
    with TestClient(api.app) as client:
        body = client.get("/api/setup/status").json()

    assert body["initialized"] is False
    names = [check["name"] for check in body["dependencies"]]
    assert names == ["database", "redis", "storage", "ollama"]
    database = body["dependencies"][0]
    assert database["ok"] is True


def test_setup_owner_success_sets_cookie_and_signs_in() -> None:
    with TestClient(api.app) as client:
        response = client.post("/api/setup/owner", json=OWNER)

    assert response.status_code == 201
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "smart_rag_session=" in cookie

    with TestClient(api.app) as client:
        me = client.get("/api/session")

    # New TestClient has no cookie jar; the session cookie must not leak across.
    assert me.status_code == 401


def test_setup_owner_then_authenticated_roundtrip() -> None:
    with TestClient(api.app) as client:
        client.post("/api/setup/owner", json=OWNER)
        me = client.get("/api/session")

    assert me.status_code == 200
    body = me.json()
    assert body["email"] == OWNER["email"]
    assert body["role"] == "owner"
    assert body["must_change_password"] is False


def test_second_owner_refused_with_conflict() -> None:
    with TestClient(api.app) as client:
        client.post("/api/setup/owner", json=OWNER)
        response = client.post(
            "/api/setup/owner", json={"email": "second@example.com", "password": "other-pass-2"}
        )

    assert response.status_code == 409
    assert "reset-owner-password" in response.json()["detail"]


def test_setup_owner_dependency_failure_is_actionable_and_persists_nothing(
    monkeypatch,
) -> None:
    failing = SetupReadiness(
        checks=[
            DependencyCheck(
                name="database",
                ok=False,
                detail="ConnectionRefusedError",
                remediation="run: make up",
            ),
            DependencyCheck(name="redis", ok=True, detail="reachable"),
        ]
    )

    async def fake_readiness() -> SetupReadiness:
        return failing

    monkeypatch.setattr(auth_routes, "readiness", fake_readiness)
    with TestClient(api.app) as client:
        response = client.post("/api/setup/owner", json=OWNER)

    assert response.status_code == 503
    checks = response.json()["detail"]["checks"]
    assert checks[0]["remediation"] == "run: make up"
    assert "database" in response.json()["detail"]["message"]

    async def users() -> int:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.connect() as connection:
                return (
                    await connection.execute(text("SELECT count(*) FROM users"))
                ).scalar_one()
        finally:
            await engine.dispose()

    assert asyncio.run(users()) == 0


def test_sign_in_wrong_password_is_generic_401() -> None:
    with TestClient(api.app) as client:
        client.post("/api/setup/owner", json=OWNER)
        response = client.post(
            "/api/session", json={"email": OWNER["email"], "password": "wrong-password"}
        )

    assert response.status_code == 401
    assert "locked" in response.json()["detail"]


def test_sign_in_and_me_roundtrip() -> None:
    with TestClient(api.app) as client:
        client.post("/api/setup/owner", json=OWNER)
        client.delete("/api/session")  # clear setup auto-sign-in
        sign_in = client.post("/api/session", json=OWNER)
        me = client.get("/api/session")

    assert sign_in.status_code == 200
    assert me.status_code == 200
    assert me.json()["email"] == OWNER["email"]


def test_session_without_cookie_is_401() -> None:
    with TestClient(api.app) as client:
        response = client.get("/api/session")

    assert response.status_code == 401


def test_sign_out_revokes_session() -> None:
    with TestClient(api.app) as client:
        client.post("/api/setup/owner", json=OWNER)
        assert client.delete("/api/session").status_code == 204
        assert client.get("/api/session").status_code == 401


def test_cross_origin_mutation_refused() -> None:
    with TestClient(api.app) as client:
        response = client.post(
            "/api/session",
            json=OWNER,
            headers={"Origin": "http://evil.example.com"},
        )

    assert response.status_code == 403
