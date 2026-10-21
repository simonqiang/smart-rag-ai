"""Task 25: /api/feedback and /api/analytics/summary — feedback is per-user,
analytics is owner/admin (members denied), payloads stay count-only."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api import main as api
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from identity_access.setup import create_first_owner
from retrieval_answering.conversation import create_conversation, record_exchange

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}
ADMIN = {"email": "admin@example.com", "password": "admin-password-1"}
MEMBER = {"email": "member@example.com", "password": "member-password-1"}

TABLES = (
    "answer_feedback, conversation_messages, conversations, index_chunks, "
    "index_generations, source_versions, sources, collections, invitations, "
    "sessions, users, workspaces, jobs, outbox"
)


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from pathlib import Path

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
            context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
            )
            thread = await create_conversation(uow, context, title="policy thread")
            await record_exchange(
                uow, EventWriter(), context=context, conversation_id=thread,
                question="what is the refund policy",
                answer_text="Refunds take 14 days [1:refund window].",
                citations=[], language="en", request_id="req-1",
                insufficient_evidence=False,
            )
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id}

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
        "/api/users/invitations", json={"email": email, "role": role, "collections": []},
    )
    assert invitation.status_code == 201, invitation.text
    client.post("/api/session/logout")
    accepted = client.post(
        "/api/invitations/accept",
        json={"token": invitation.json()["token"], "password": password},
    )
    assert accepted.status_code in (200, 201), accepted.text
    client.post("/api/session/logout")


def test_feedback_requires_a_session(client, workspace) -> None:
    response = client.post("/api/feedback", json={"request_id": "req-1", "helpful": True})
    assert response.status_code == 401


def test_feedback_records_and_accepts_duplicates(client, workspace) -> None:
    _sign_in(client, OWNER)
    assert client.post(
        "/api/feedback", json={"request_id": "req-1", "helpful": True},
    ).status_code == 200
    assert client.post(
        "/api/feedback", json={"request_id": "req-1", "helpful": False},
    ).status_code == 200

    summary = client.get("/api/analytics/summary")
    assert summary.json()["helpful"] == 0
    assert summary.json()["unhelpful"] == 1


def test_feedback_on_unknown_request_is_a_generic_404(client, workspace) -> None:
    _sign_in(client, OWNER)
    response = client.post(
        "/api/feedback", json={"request_id": "no-such", "helpful": True},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "no such answer"}


def test_summary_is_owner_and_admin_readable_member_denied(client, workspace) -> None:
    _invite_and_accept(client, ADMIN["email"], ADMIN["password"], "admin")
    _invite_and_accept(client, MEMBER["email"], MEMBER["password"], "member")

    _sign_in(client, MEMBER)
    assert client.get("/api/analytics/summary").status_code == 403

    _sign_in(client, ADMIN)
    assert client.get("/api/analytics/summary").status_code == 200

    _sign_in(client, OWNER)
    response = client.get("/api/analytics/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["exchanges"] == 1
    assert body["answered"] == 1
    assert set(body) == {
        "exchanges", "answered", "insufficient_evidence",
        "unanswered_conversations", "helpful", "unhelpful",
    }
    assert "refund" not in response.text
