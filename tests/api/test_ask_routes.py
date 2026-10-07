"""Task 14c: /api/ask — grounding, streaming, resume, and error mapping."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.contracts import GenerationResult, ProviderUnavailableError
from ai_providers.fakes import FakeEmbeddingProvider, FakeGenerationProvider
from apps.api import main as api
from apps.api.routes import ask as ask_routes
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from identity_access.setup import create_first_owner

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}
TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)
SHARED_TEXT = "Public onboarding steps for every employee joining the support team."
GROUND_ANSWER = (
    "Follow the public onboarding steps "
    f"[1:{SHARED_TEXT}]."
)
DISPLAY_ANSWER = "Follow the public onboarding steps [1]."

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
def workspace(tmp_path: Path):
    async def create() -> dict:
        settings = Settings.load()
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        uow = UnitOfWork(engine)
        store = ObjectStore(tmp_path / "store")
        try:
            owner = await create_first_owner(uow, EventWriter(), **OWNER)
            context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
                collection_ids=[],
            )
            from source_catalog.catalog import create_collection

            shared = await create_collection(uow, EventWriter(), context=context, name="Shared")
            context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
                collection_ids=[shared.collection_id],
            )
            source_id = await upload_and_index(
                uow, store, context, collection_id=shared.collection_id,
                name="onboarding", data=SHARED_TEXT.encode(),
            )
        finally:
            await engine.dispose()
        return {
            "owner_id": owner.user_id,
            "source_id": source_id,
            "fake_generation": FakeGenerationProvider(
                script=[GenerationResult(GROUND_ANSWER, "fake-chat")]
            ),
            "fake_embeddings": FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
        }

    state = asyncio.run(create())
    ask_routes._generation = state["fake_generation"]
    ask_routes._embeddings = state["fake_embeddings"]
    ask_routes._profile = TEST_PROFILE
    ask_routes._rewriter = None
    yield state
    ask_routes._generation = None
    ask_routes._embeddings = None
    ask_routes._profile = None
    ask_routes._rewriter = None


async def upload_and_index(uow, store, context, *, collection_id: str,
                           name: str, data: bytes) -> str:
    from ingestion.extraction import SourceObject, extract
    from ingestion.uploads import register_upload
    from knowledge_index.generations import activate_generation
    from knowledge_index.indexer import index_version

    accepted = await register_upload(
        uow, EventWriter(), store, context=context, collection_id=collection_id,
        name=name, filename=f"{name}.txt", data=data,
    )
    document = extract(SourceObject(
        source_version_id=accepted.source_version_id, media_type=accepted.media_type,
        filename=f"{name}.txt", data=store.get(accepted.checksum),
    ))
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
            {"id": accepted.source_version_id},
        )
    staged = await index_version(
        uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
        workspace_id=context.workspace_id,
        source_version_id=accepted.source_version_id,
        document=document, profile=TEST_PROFILE,
    )
    await activate_generation(
        uow, EventWriter(), staged.generation_id, workspace_id=context.workspace_id,
    )
    return accepted.source_id


def _sign_in(client: TestClient) -> None:
    response = client.post("/api/session", json=OWNER)
    assert response.status_code == 200, response.text


def _sse_frames(body: str) -> list[dict]:
    return [
        json.loads(chunk.removeprefix("data: "))
        for chunk in body.split("\n\n")
        if chunk.startswith("data: ")
    ]


def test_ask_requires_a_session(workspace) -> None:
    with TestClient(api.app) as client:
        response = client.post("/api/ask", json={"question": SHARED_TEXT})
    assert response.status_code == 401


def test_owner_gets_a_grounded_answer_with_citations(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.post("/api/ask", json={"question": "How do I onboard?"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"]["text"] == DISPLAY_ANSWER
    assert body["question_language"] == "en"
    assert body["insufficient_evidence"] is False
    citation = body["answer"]["citations"][0]
    assert citation["source_name"] == "onboarding"
    assert citation["quote"] == SHARED_TEXT
    assert body["provider"] == {"name": "ollama", "local": True}
    assert body["request_id"] and body["conversation_id"]


def test_exchange_is_persisted_with_safe_audit_metadata(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.post("/api/ask", json={"question": "How do I onboard?"})
    conversation_id = response.json()["conversation_id"]

    async def verify() -> tuple[list, list]:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                messages = (
                    await connection.execute(
                        text("SELECT role FROM conversation_messages "
                             "WHERE conversation_id = CAST(:id AS uuid) ORDER BY number"),
                        {"id": conversation_id},
                    )
                ).scalars().all()
                audits = (
                    await connection.execute(
                        text("SELECT metadata FROM audit_events "
                             "WHERE type = 'question.asked'"),
                    )
                ).scalars().all()
        finally:
            await engine.dispose()
        return list(messages), list(audits)

    messages, audits = asyncio.run(verify())
    assert messages == ["user", "assistant"]
    assert len(audits) == 1
    assert "How do I onboard?" not in json.dumps(audits)


def test_empty_question_maps_to_400(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.post("/api/ask", json={"question": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "question is empty"


def test_unanswerable_question_skips_generation(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.post(
            "/api/ask", json={"question": "zzqq unreadable pension plan xyzz"}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["insufficient_evidence"] is True
    assert body["answer"]["citations"] == []
    assert workspace["fake_generation"].requests == []


def test_embedding_outage_maps_to_actionable_503(workspace) -> None:
    ask_routes._embeddings = FakeEmbeddingProvider(
        TEST_PROFILE.embedding_dimensions,
        script=[ProviderUnavailableError("embed endpoint down")],
    )
    try:
        with TestClient(api.app) as client:
            _sign_in(client)
            response = client.post("/api/ask", json={"question": SHARED_TEXT})
    finally:
        ask_routes._embeddings = workspace["fake_embeddings"]

    assert response.status_code == 503
    assert "AI provider unavailable" in response.json()["detail"]


def test_uncited_generation_maps_to_502_and_persists_nothing(workspace) -> None:
    ask_routes._generation = FakeGenerationProvider(default_text="uncited guess")
    try:
        with TestClient(api.app) as client:
            _sign_in(client)
            response = client.post("/api/ask", json={"question": SHARED_TEXT})
    finally:
        ask_routes._generation = workspace["fake_generation"]

    assert response.status_code == 502
    assert "properly cited" in response.json()["detail"]

    async def count_messages() -> int:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                return int((
                    await connection.execute(text("SELECT count(*) FROM conversation_messages"))
                ).scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(count_messages()) == 0


def test_streaming_mode_emits_typed_frames_in_order(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.post(
            "/api/ask",
            json={"question": "How do I onboard?"},
            headers={"Accept": "text/event-stream"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(response.text)
    assert [frame["kind"] for frame in frames] == [
        "started", "delta", "citation", "completed",
    ]
    request_id = frames[0]["request_id"]
    assert all(frame["request_id"] == request_id for frame in frames)
    assert [frame["seq"] for frame in frames] == [1, 2, 3, 4]
    assert frames[-1]["text"] == DISPLAY_ANSWER
    assert frames[-1]["insufficient_evidence"] is False
    assert frames[2]["citation"]["quote"] == SHARED_TEXT
    assert frames[-1]["provider"] == {"name": "ollama", "local": True}

    # SSE clients continue the thread from the started frame alone.
    conversation_id = frames[0]["conversation_id"]
    assert conversation_id

    async def persisted() -> int:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                return int((
                    await connection.execute(
                        text("SELECT count(*) FROM conversation_messages "
                             "WHERE conversation_id = CAST(:id AS uuid)"),
                        {"id": conversation_id},
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(persisted()) == 2


def test_generation_timeout_streams_a_safe_error_frame(workspace) -> None:
    ask_routes._generation = FakeGenerationProvider(
        script=[ProviderUnavailableError("hostLEAK down", reason="timeout")]
    )
    try:
        with TestClient(api.app) as client:
            _sign_in(client)
            response = client.post(
                "/api/ask",
                json={"question": SHARED_TEXT},
                headers={"Accept": "text/event-stream"},
            )
    finally:
        ask_routes._generation = workspace["fake_generation"]

    frames = _sse_frames(response.text)
    assert [frame["kind"] for frame in frames] == ["started", "error"]
    assert frames[-1]["reason"] == "timeout"
    assert "hostLEAK" not in response.text


def test_resume_replays_a_completed_answer(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        asked = client.post("/api/ask", json={"question": "How do I onboard?"})
        request_id = asked.json()["request_id"]

        resumed = client.get(f"/api/ask/{request_id}/events")

    assert resumed.status_code == 200
    frames = _sse_frames(resumed.text)
    assert [frame["kind"] for frame in frames] == ["started", "delta", "citation", "completed"]
    assert frames[1]["text"] == DISPLAY_ANSWER
    assert [frame["seq"] for frame in frames] == [1, 2, 3, 4]

    with TestClient(api.app) as client:
        _sign_in(client)
        missing = client.get("/api/ask/does-not-exist/events")
    frames = _sse_frames(missing.text)
    assert frames[-1]["kind"] == "error"
    assert frames[-1]["reason"] == "unknown_request"


def test_rewriter_runs_before_retrieval_with_bounded_history(workspace) -> None:
    class RecordingRewriter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def rewrite(self, question: str, history):
            self.calls.append((question, len(history.messages)))
            return f"rewritten({question})"

    rewriter = RecordingRewriter()
    ask_routes._rewriter = rewriter
    try:
        with TestClient(api.app) as client:
            _sign_in(client)
            first = client.post("/api/ask", json={"question": "first question"})
            conversation_id = first.json()["conversation_id"]
            client.post("/api/ask", json={
                "question": "second question", "conversation_id": conversation_id,
            })
    finally:
        ask_routes._rewriter = None

    assert [question for question, _ in rewriter.calls] == [
        "first question", "second question",
    ]
    assert [count for _, count in rewriter.calls] == [0, 2]
    # Retrieval embedded the rewritten queries, in order (indexing used other fakes).
    assert workspace["fake_embeddings"].requests[0] == ["rewritten(first question)"]
    assert workspace["fake_embeddings"].requests[1] == ["rewritten(second question)"]


def test_unknown_conversation_maps_to_generic_404(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client)
        response = client.post(
            "/api/ask",
            json={"question": SHARED_TEXT, "conversation_id": "00000000-0000-0000-0000-000000000000"},
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "no such conversation"


def test_provider_seams_compose_from_real_settings(workspace, monkeypatch) -> None:
    monkeypatch.setattr(ask_routes, "_generation", None)
    monkeypatch.setattr(ask_routes, "_embeddings", None)
    monkeypatch.setattr(ask_routes, "_profile", None)

    assert callable(ask_routes.generation().generate)
    assert callable(ask_routes.embeddings().embed)
    assert ask_routes.profile() == Settings.load().active_profile
