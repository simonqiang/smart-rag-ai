"""Task 13: hybrid search over the API — auth, grants, error mapping."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.contracts import ProviderUnavailableError
from ai_providers.fakes import FakeEmbeddingProvider
from apps.api import main as api
from apps.api.routes import search as search_routes
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
SECRET_TEXT = "Executive redundancy payouts negotiated behind closed doors."

TABLES = (
    "index_chunks, index_generations, source_versions, sources, collections, "
    "invitations, sessions, users, workspaces"
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
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner"
            )
            shared = await create_collection_via_service(
                uow, context, name="Shared"
            )
            restricted = await create_collection_via_service(
                uow, context, name="Restricted"
            )
            shared_source = await upload_and_index(
                engine, store, context, collection_id=shared["id"],
                name="onboarding", data=SHARED_TEXT.encode(),
            )
            await upload_and_index(
                engine, store, context, collection_id=restricted["id"],
                name="redundancy", data=SECRET_TEXT.encode(),
            )
        finally:
            await engine.dispose()
        return {
            "shared_source_id": shared_source,
            "fake_embeddings": FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
        }

    state = asyncio.run(create())
    search_routes._embeddings = state["fake_embeddings"]
    search_routes._profile = TEST_PROFILE
    yield state
    search_routes._embeddings = None
    search_routes._profile = None


async def create_collection_via_service(uow, context, *, name: str) -> dict:
    from source_catalog.catalog import create_collection

    created = await create_collection(uow, EventWriter(), context=context, name=name)
    return {"id": created.collection_id}


async def upload_and_index(engine, store, context, *, collection_id: str,
                           name: str, data: bytes) -> str:
    from ingestion.extraction import SourceObject, extract
    from ingestion.uploads import register_upload
    from knowledge_index.generations import activate_generation
    from knowledge_index.indexer import index_version

    uow = UnitOfWork(engine)
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


def test_search_requires_a_session(workspace) -> None:
    with TestClient(api.app) as client:
        response = client.post("/api/search", json={"query": SHARED_TEXT})
    assert response.status_code == 401


def test_owner_searches_and_gets_evidence_with_locations(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        response = client.post("/api/search", json={"query": SHARED_TEXT})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query_language"] == "en"
    assert body["confident"] is True
    top = body["items"][0]
    assert top["text"].startswith("Public onboarding")
    assert top["rank"] == 1
    assert top["block_start"] == 0
    assert top["source_name"] == "onboarding"


def test_empty_query_maps_to_400(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        response = client.post("/api/search", json={"query": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "query is empty"


def test_provider_outage_maps_to_actionable_503(workspace) -> None:
    search_routes._embeddings = FakeEmbeddingProvider(
        TEST_PROFILE.embedding_dimensions,
        script=[ProviderUnavailableError("embed endpoint down")],
    )
    try:
        with TestClient(api.app) as client:
            _sign_in(client, OWNER)
            response = client.post("/api/search", json={"query": SHARED_TEXT})
    finally:
        search_routes._embeddings = workspace["fake_embeddings"]

    assert response.status_code == 503
    assert "embedding provider unavailable" in response.json()["detail"]


def test_source_filter_passes_through_the_route(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        response = client.post("/api/search", json={
            "query": SHARED_TEXT, "source_id": workspace["shared_source_id"],
        })

    assert response.status_code == 200
    assert {item["source_id"] for item in response.json()["items"]} == {
        workspace["shared_source_id"]
    }


def _sign_in(client: TestClient, person: dict) -> None:
    response = client.post("/api/session", json=person)
    assert response.status_code == 200, response.text


def test_provider_seams_compose_from_real_settings(workspace, monkeypatch) -> None:
    from foundation.config import Settings

    monkeypatch.setattr(search_routes, "_embeddings", None)
    monkeypatch.setattr(search_routes, "_profile", None)

    assert callable(search_routes.embeddings().embed)
    assert search_routes.profile() == Settings.load().active_profile
