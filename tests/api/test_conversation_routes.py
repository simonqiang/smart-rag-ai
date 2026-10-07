"""Task 15c: /api/conversations — list, reopen, rename, delete own threads."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
from retrieval_answering.conversation import (
    create_conversation,
    record_exchange,
)

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}

TABLES = (
    "conversation_messages, conversations, index_chunks, index_generations, "
    "source_versions, sources, collections, invitations, sessions, users, workspaces"
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
            context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
            )
            first = await create_conversation(uow, context, title="first thread")
            await record_exchange(
                uow, EventWriter(), context=context, conversation_id=first,
                question="q1", answer_text="a1 [1:quote]",
                citations=[{"label": 1, "chunk_id": "c1", "quote": "quote"}],
                language="en", request_id="req-1", insufficient_evidence=False,
            )
            second = await create_conversation(uow, context, title="second thread")
        finally:
            await engine.dispose()
        return {"first": first, "second": second}

    return asyncio.run(create())


def _sign_in(client: TestClient) -> None:
    response = client.post("/api/session", json=OWNER)
    assert response.status_code == 200, response.text


def test_conversations_require_a_session(workspace) -> None:
    with TestClient(api.app) as client:
        response = client.get("/api/conversations")
    assert response.status_code == 401


def test_list_conversations_is_newest_first(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.get("/api/conversations")
    assert response.status_code == 200
    titles = [row["title"] for row in response.json()]
    assert titles == ["second thread", "first thread"]


def test_reopen_returns_messages_with_citations(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.get(f"/api/conversations/{workspace['first']}")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "first thread"
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["citations"][0]["quote"] == "quote"


def test_unknown_conversation_is_a_generic_404(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.get(
            "/api/conversations/00000000-0000-0000-0000-000000000000"
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "no such conversation"


def test_rename_updates_the_title(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.put(
            f"/api/conversations/{workspace['second']}", json={"title": "renamed"}
        )
        assert response.status_code == 204
        titles = [row["title"] for row in client.get("/api/conversations").json()]
    assert "renamed" in titles


def test_delete_removes_the_thread_and_messages(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        assert client.delete(f"/api/conversations/{workspace['second']}").status_code == 204
        titles = [row["title"] for row in client.get("/api/conversations").json()]

        async def count() -> int:
            engine = create_async_engine(Settings.load().database_url)
            try:
                async with engine.begin() as connection:
                    return int((
                        await connection.execute(text("SELECT count(*) FROM conversation_messages"))
                    ).scalar_one())
            finally:
                await engine.dispose()

        messages = asyncio.run(count())
    assert titles == ["first thread"]
    assert messages == 2


def test_empty_list_returns_empty_state(workspace) -> None:
    async def clear() -> None:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM conversations"))
        finally:
            await engine.dispose()

    asyncio.run(clear())
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.get("/api/conversations")
    assert response.status_code == 200
    assert response.json() == []


def test_rename_rejects_empty_titles_and_unknown_ids(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        empty = client.put(f"/api/conversations/{workspace['first']}", json={"title": "   "})
        missing = client.put(
            "/api/conversations/00000000-0000-0000-0000-000000000000", json={"title": "x"}
        )
    assert empty.status_code == 400
    assert empty.json()["detail"] == "title is empty"
    assert missing.status_code == 404


def test_delete_unknown_conversation_is_a_generic_404(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        missing = client.delete("/api/conversations/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
