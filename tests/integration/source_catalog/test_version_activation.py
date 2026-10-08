"""Task 16: archive, replacement cutover, and rollback (live Compose PostgreSQL)."""

import asyncio
import uuid
from pathlib import Path

import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import (
    AccessContext,
    AccessDenied,
    accessible_collection_ids,
)
from identity_access.setup import create_first_owner
from ingestion.extraction import SourceObject, extract
from ingestion.uploads import register_upload
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from retrieval_answering.retrieval import retrieve
from source_catalog.catalog import SourceNotFound, create_collection
from source_catalog.version_service import (
    VersionNotReady,
    activate_ready_version,
    archive_source,
    list_versions,
    rollback_as_new_version,
    stage_replacement,
    unarchive_source,
)

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

OLD_TEXT = "The approved travel policy allows economy class flights for trips under six hours."
NEW_TEXT = "The revised travel policy books the lowest logical fare regardless of flight duration."

LIFECYCLE_TABLES = (
    "index_chunks, index_generations, source_versions, sources, collections, "
    "invitations, sessions, users, workspaces, jobs, outbox"
)


def _run(db: Settings, coro_factory, store_root: Path | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        store = ObjectStore(store_root) if store_root else None
        try:
            return await coro_factory(uow, engine, store)
        finally:
            await engine.dispose()

    return asyncio.run(run())


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
            await connection.execute(text(f"TRUNCATE {LIFECYCLE_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


@pytest.fixture()
def store_root(db: Settings, tmp_path: Path) -> Path:
    return tmp_path / "store"


async def _pipeline(
    uow, store: ObjectStore, context: AccessContext, accepted, filename: str,
) -> str:
    """Shared extract → index → activate tail of the ingestion pipeline."""
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
    staged = await index_version(
        uow, FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
        workspace_id=context.workspace_id,
        source_version_id=accepted.source_version_id,
        document=document,
        profile=TEST_PROFILE,
    )
    await activate_generation(
        uow, EventWriter(), staged.generation_id,
        workspace_id=context.workspace_id,
    )
    return accepted.source_version_id


@pytest.fixture()
def workspace(db: Settings, store_root: Path) -> dict:
    """Owner, an ungranted member, a collection, and one indexed source."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            uow = UnitOfWork(engine)
            owner = await create_first_owner(
                uow, EventWriter(), email="owner@example.com", password="owner-password-1"
            )
            member_context = AccessContext(
                user_id=str(uuid.uuid4()), workspace_id=owner.workspace_id, role="member",
            )
            shared = await create_collection(
                uow, EventWriter(),
                context=AccessContext(
                    user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
                ),
                name="Shared",
            )
            owner_context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
                collection_ids=await accessible_collection_ids(
                    uow, user_id=owner.user_id, workspace_id=owner.workspace_id,
                ),
            )
            accepted = await register_upload(
                uow, EventWriter(), ObjectStore(store_root),
                context=owner_context, collection_id=shared.collection_id,
                name="travel-policy", filename="travel.txt",
                data=OLD_TEXT.encode("utf-8"),
            )
            version_id = await _pipeline(
                uow, ObjectStore(store_root), owner_context, accepted, "travel.txt",
            )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "collection_id": shared.collection_id,
            "owner_context": owner_context,
            "member_context": member_context,
            "source_id": accepted.source_id,
            "version_id": version_id,
        }

    return asyncio.run(create())


def _owner(workspace: dict) -> AccessContext:
    return workspace["owner_context"]


def _ask_texts(db: Settings, workspace: dict, query: str) -> list[str]:
    """Evidence chunk texts for a query. Fake embeddings carry no semantics,
    so relevance asserts use content markers, not rankings."""

    async def run() -> list[str]:
        engine = create_async_engine(db.database_url)
        try:
            evidence = await retrieve(
                UnitOfWork(engine), FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
                context=_owner(workspace), query=query, profile=TEST_PROFILE,
            )
            return [item.text for item in evidence.items]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _asks(db: Settings, workspace: dict, query: str) -> tuple[list[str], list[str]]:
    """(source_ids, texts) for the query."""
    async def run() -> tuple[list[str], list[str]]:
        engine = create_async_engine(db.database_url)
        try:
            evidence = await retrieve(
                UnitOfWork(engine), FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
                context=_owner(workspace), query=query, profile=TEST_PROFILE,
            )
            return (
                [item.source_id for item in evidence.items],
                [item.text for item in evidence.items],
            )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _version_state(db: Settings, version_id: str) -> str:
    async def run() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return str((
                    await connection.execute(
                        text("SELECT state FROM source_versions WHERE id = :id"),
                        {"id": version_id},
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _version_row(db: Settings, version_id: str) -> dict:
    async def run() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT object_sha256, size_bytes, media_type, filename "
                            "FROM source_versions WHERE id = :id"
                        ),
                        {"id": version_id},
                    )
                ).mappings().one()
                return dict(row)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _active_versions(db: Settings, source_id: str) -> list[str]:
    async def run() -> list[str]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return [
                    str(row) for row in (
                        await connection.execute(
                            text(
                                "SELECT id FROM source_versions "
                                "WHERE source_id = :id AND state = 'active'"
                            ),
                            {"id": source_id},
                        )
                    ).scalars().all()
                ]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _audits(db: Settings, event_type: str) -> list[dict]:
    async def run() -> list[dict]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT actor::text AS actor, subject::text AS subject, metadata "
                            "FROM audit_events WHERE type = :type"
                        ),
                        {"type": event_type},
                    )
                ).mappings().all()
                return [dict(row) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _replacer(workspace: dict, store_root: Path, ready: bool):
    """Coro factory staging a replacement into the workspace source,
    optionally driven through indexing to the 'ready' state."""

    async def stage(uow, engine, store) -> dict:
        context = _owner(workspace)
        replacement_store = ObjectStore(store_root)
        accepted = await stage_replacement(
            uow, EventWriter(), replacement_store,
            context=context, source_id=workspace["source_id"],
            filename="travel-v2.txt", data=NEW_TEXT.encode("utf-8"),
        )
        if ready:
            await _pipeline(uow, replacement_store, context, accepted, "travel-v2.txt")
        return {"source_id": accepted.source_id, "version_id": accepted.source_version_id}

    return stage


def test_first_indexed_version_activates_automatically(db, workspace) -> None:
    assert _version_state(db, workspace["version_id"]) == "active"
    assert _active_versions(db, workspace["source_id"]) == [workspace["version_id"]]


def test_archive_excludes_retrieval_immediately_then_unarchive_restores(db, workspace) -> None:
    sources, _ = _asks(db, workspace, "travel policy flights")
    assert sources == [workspace["source_id"]]

    def archive(uow, engine, store) -> None:
        return archive_source(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )

    _run(db, archive)
    assert _asks(db, workspace, "travel policy flights") == ([], [])
    assert _version_state(db, workspace["version_id"]) == "active"  # version untouched

    def unarchive(uow, engine, store) -> None:
        return unarchive_source(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )

    _run(db, unarchive)
    sources, _ = _asks(db, workspace, "travel policy flights")
    assert sources == [workspace["source_id"]]
    assert len(_audits(db, "source.archived")) == 1
    assert len(_audits(db, "source.unarchived")) == 1


def test_archive_requires_admin(db, workspace) -> None:
    def archive(uow, engine, store) -> None:
        return archive_source(
            uow, EventWriter(),
            context=workspace["member_context"], source_id=workspace["source_id"],
        )

    with pytest.raises(AccessDenied):
        _run(db, archive)


def test_archive_unknown_or_foreign_source_is_not_found(db, workspace) -> None:
    def foreign(uow, engine, store) -> None:
        return archive_source(
            uow, EventWriter(), context=_owner(workspace), source_id=str(uuid.uuid4()),
        )

    with pytest.raises(SourceNotFound):
        _run(db, foreign)


def test_single_active_invariant_is_enforced_by_database(db, workspace) -> None:
    async def violate() -> None:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO source_versions (id, workspace_id, source_id, "
                        "object_sha256, size_bytes, media_type, filename, state, created_by) "
                        "VALUES (:id, :workspace_id, :source_id, 'sha-second', 1, "
                        "'text/plain', 'second.txt', 'active', :created_by)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "workspace_id": workspace["workspace_id"],
                        "source_id": workspace["source_id"],
                        "created_by": workspace["owner_id"],
                    },
                )
        finally:
            await engine.dispose()

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        asyncio.run(violate())


def test_stage_replacement_keeps_old_version_live(db, workspace, store_root) -> None:
    def stage(uow, engine, store) -> object:
        return stage_replacement(
            uow, EventWriter(), ObjectStore(store_root),
            context=_owner(workspace), source_id=workspace["source_id"],
            filename="travel-v2.txt", data=NEW_TEXT.encode("utf-8"),
        )

    accepted = _run(db, stage, store_root)
    assert accepted.source_id == workspace["source_id"]
    assert accepted.source_version_id != workspace["version_id"]
    assert _version_state(db, accepted.source_version_id) == "uploaded"
    sources, _ = _asks(db, workspace, "travel policy flights")
    assert sources == [workspace["source_id"]]

    uploads = _audits(db, "source.uploaded")
    assert len(uploads) == 2
    assert uploads[-1]["metadata"]["replacement_of"] == workspace["source_id"]


def test_ready_replacement_stays_out_of_retrieval_until_cutover(db, workspace, store_root) -> None:
    replacement = _run(db, _replacer(workspace, store_root, ready=True), store_root)
    assert _version_state(db, replacement["version_id"]) == "indexed"  # ready, not live
    assert _active_versions(db, workspace["source_id"]) == [workspace["version_id"]]
    _, texts = _asks(db, workspace, "lowest logical fare")
    assert not any("lowest logical fare" in text for text in texts)  # new content invisible
    sources, _ = _asks(db, workspace, "travel policy flights")
    assert sources == [workspace["source_id"]]


def test_activate_ready_version_cuts_over_atomically(db, workspace, store_root) -> None:
    replacement = _run(db, _replacer(workspace, store_root, ready=True), store_root)
    old_version = workspace["version_id"]

    def activate(uow, engine, store) -> None:
        return activate_ready_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=replacement["version_id"],
        )

    _run(db, activate)
    assert _version_state(db, replacement["version_id"]) == "active"
    assert _version_state(db, old_version) == "superseded"
    assert _active_versions(db, workspace["source_id"]) == [replacement["version_id"]]
    sources, texts = _asks(db, workspace, "lowest logical fare")
    assert sources == [workspace["source_id"]]  # keyword channel guarantees a hit
    assert any("lowest logical fare" in text for text in texts)  # new content visible
    _, old_texts = _asks(db, workspace, "economy class flights")
    assert not any("economy class" in text for text in old_texts)  # old content gone

    activations = _audits(db, "source.version_activated")
    assert len(activations) == 1
    assert activations[0]["subject"] == replacement["version_id"]
    assert activations[0]["actor"] == workspace["owner_id"]


def test_activate_refuses_version_that_is_not_ready(db, workspace) -> None:
    def activate(uow, engine, store) -> None:
        return activate_ready_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=workspace["version_id"],
        )

    with pytest.raises(VersionNotReady):
        _run(db, activate)  # the live version is 'active', not 'indexed'


def test_concurrent_cutover_admits_exactly_one_winner(db, workspace, store_root) -> None:
    replacement = _run(db, _replacer(workspace, store_root, ready=True), store_root)

    async def race() -> list[object]:
        async def attempt() -> object:
            engine = create_async_engine(db.database_url)
            try:
                await activate_ready_version(
                    UnitOfWork(engine), EventWriter(), context=_owner(workspace),
                    source_id=workspace["source_id"], version_id=replacement["version_id"],
                )
                return "ok"
            except Exception:  # noqa: BLE001 - the race outcome is the assertion
                return "lost"
            finally:
                await engine.dispose()

        return list(await asyncio.gather(attempt(), attempt()))

    outcomes = asyncio.run(race())
    assert sorted(outcomes) == ["lost", "ok"]
    assert _active_versions(db, workspace["source_id"]) == [replacement["version_id"]]


def test_rollback_creates_new_version_from_history(db, workspace, store_root) -> None:
    replacement = _run(db, _replacer(workspace, store_root, ready=True), store_root)

    def activate(uow, engine, store) -> None:
        return activate_ready_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=replacement["version_id"],
        )

    _run(db, activate)

    def rollback(uow, engine, store) -> object:
        return rollback_as_new_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=workspace["version_id"],
        )

    rolled_back = _run(db, rollback)
    assert rolled_back.version_id != workspace["version_id"]
    assert rolled_back.source_id == workspace["source_id"]
    assert rolled_back.state == "uploaded"

    restored = _version_row(db, rolled_back.version_id)
    original = _version_row(db, workspace["version_id"])
    assert restored["object_sha256"] == original["object_sha256"]
    assert restored["filename"] == original["filename"]
    assert _version_state(db, rolled_back.version_id) == "uploaded"
    assert len(_audits(db, "source.rollback_staged")) == 1

    async def pending_job() -> str | None:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return (
                    await connection.execute(
                        text(
                            "SELECT topic FROM outbox WHERE published_at IS NULL "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                ).scalar_one_or_none()
        finally:
            await engine.dispose()

    assert asyncio.run(pending_job()) == "source_extract"
    # The bad content remains live until the restored version cuts over.
    assert _active_versions(db, workspace["source_id"]) == [replacement["version_id"]]


def test_rollback_refuses_version_that_never_validated(db, workspace) -> None:
    async def stage_raw() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                version_id = str(uuid.uuid4())
                await connection.execute(
                    text(
                        "INSERT INTO source_versions (id, workspace_id, source_id, "
                        "object_sha256, size_bytes, media_type, filename, state, created_by) "
                        "VALUES (:id, :workspace_id, :source_id, 'sha-raw', 1, "
                        "'text/plain', 'raw.txt', 'uploaded', :created_by)"
                    ),
                    {
                        "id": version_id,
                        "workspace_id": workspace["workspace_id"],
                        "source_id": workspace["source_id"],
                        "created_by": workspace["owner_id"],
                    },
                )
                return version_id
        finally:
            await engine.dispose()

    raw_version = asyncio.run(stage_raw())

    def rollback(uow, engine, store) -> object:
        return rollback_as_new_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=raw_version,
        )

    with pytest.raises(VersionNotReady):
        _run(db, rollback)


def test_archive_is_idempotent(db, workspace) -> None:
    async def archive_twice(uow, engine, store) -> None:
        for _ in range(2):
            await archive_source(
                uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
            )

    _run(db, archive_twice)
    archives = _audits(db, "source.archived")
    assert len(archives) == 1  # the second call changed nothing

    async def unarchive_twice(uow, engine, store) -> None:
        for _ in range(2):
            await unarchive_source(
                uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
            )

    _run(db, unarchive_twice)
    assert len(_audits(db, "source.unarchived")) == 1


def test_stage_replacement_refuses_missing_or_frozen_target(db, workspace, store_root) -> None:
    def stage(uow, engine, store) -> object:
        return stage_replacement(
            uow, EventWriter(), ObjectStore(store_root),
            context=_owner(workspace), source_id=str(uuid.uuid4()),
            filename="travel-v2.txt", data=NEW_TEXT.encode("utf-8"),
        )

    with pytest.raises(SourceNotFound):
        _run(db, stage, store_root)

    def archive(uow, engine, store) -> object:
        return archive_source(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )

    _run(db, archive)

    def stage_archived(uow, engine, store) -> object:
        return stage_replacement(
            uow, EventWriter(), ObjectStore(store_root),
            context=_owner(workspace), source_id=workspace["source_id"],
            filename="travel-v2.txt", data=NEW_TEXT.encode("utf-8"),
        )

    with pytest.raises(SourceNotFound):
        _run(db, stage_archived, store_root)


def test_activate_and_rollback_refuse_foreign_versions(db, workspace, store_root) -> None:
    def activate(uow, engine, store) -> None:
        return activate_ready_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=str(uuid.uuid4()),
        )

    with pytest.raises(SourceNotFound):
        _run(db, activate)

    def rollback(uow, engine, store) -> object:
        return rollback_as_new_version(
            uow, EventWriter(), context=_owner(workspace),
            source_id=workspace["source_id"], version_id=str(uuid.uuid4()),
        )

    with pytest.raises(SourceNotFound):
        _run(db, rollback)


def test_list_versions_returns_full_history(db, workspace, store_root) -> None:
    replacement = _run(db, _replacer(workspace, store_root, ready=True), store_root)

    def versions(uow, engine, store) -> object:
        return list_versions(uow, context=_owner(workspace), source_id=workspace["source_id"])

    history = _run(db, versions)
    assert [v.version_id for v in history] == [
        workspace["version_id"], replacement["version_id"],
    ]
    assert [v.state for v in history] == ["active", "indexed"]
