"""Shared fixtures for knowledge-index integration tests (live Compose PostgreSQL)."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from identity_access.setup import create_first_owner
from ingestion.extraction import SourceObject, extract
from ingestion.uploads import register_upload
from source_catalog.catalog import create_collection

INDEX_TABLES = (
    "index_chunks, index_generations, source_versions, sources, collections, "
    "invitations, sessions, users, workspaces, jobs, outbox"
)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {INDEX_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


@pytest.fixture()
def store_root(db: Settings, tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture()
def workspace(db: Settings, store_root: Path) -> dict:
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        ObjectStore(store_root)
        try:
            owner = await create_first_owner(
                uow, EventWriter(), email="owner@example.com", password="owner-password-1"
            )
            collection = await create_collection(
                uow, EventWriter(),
                context=AccessContext(
                    user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner"
                ),
                name="Policies",
            )
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id, "owner_id": owner.user_id,
                "collection_id": collection.collection_id}

    return asyncio.run(create())


def _fake_embeddings(dimensions: int = 8) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dimensions)


async def _upload_and_extract(
    engine, store: ObjectStore, workspace: dict, *, data: bytes, filename: str,
) -> str:
    """Real upload path, then flip the version to ``extracted`` with a real parse."""
    uow = UnitOfWork(engine)
    context = AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"], role="owner"
    )
    accepted = await register_upload(
        uow, EventWriter(), store,
        context=context,
        collection_id=workspace["collection_id"],
        name="Handbook",
        filename=filename,
        data=data,
    )
    document = extract(SourceObject(
        source_version_id=accepted.source_version_id,
        media_type=accepted.media_type,
        filename=filename,
        data=store.get(accepted.checksum),
    ))
    async with uow.transaction() as transaction:
        await transaction.execute(
            text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
            {"id": accepted.source_version_id},
        )
    return accepted.source_version_id, document


@pytest.fixture()
def indexed(db: Settings, store_root: Path, workspace: dict) -> dict:
    """An uploaded, extracted English version ready to index (not yet indexed)."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            version_id, document = await _upload_and_extract(
                engine, ObjectStore(store_root), workspace,
                data=b"Smart RAG keeps answers grounded in cited evidence.\n"
                     b"Every claim must point at a stored passage.",
                filename="handbook.txt",
            )
        finally:
            await engine.dispose()
        return {"version_id": version_id, "document": document}

    return asyncio.run(create())
