"""Task 17: permanent source deletion — owner-only, purge, redaction, retry."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import (
    InMemoryManagedBackupInventory,
    ManagedBackup,
    ObjectStore,
)
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext, AccessDenied
from identity_access.invitations import accept_invitation, issue_invitation_token
from identity_access.setup import create_first_owner
from ingestion.extraction import SourceObject, extract
from ingestion.uploads import register_upload
from knowledge_index.generations import activate_generation
from knowledge_index.indexer import index_version
from retrieval_answering.retrieval import retrieve
from source_catalog.catalog import SourceNotFound, create_collection
from source_catalog.deletion import (
    DELETION_NOTICE,
    DeletionIncomplete,
    delete_source,
    request_permanent_deletion,
)

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

DOC_TEXT = "The approved travel policy allows economy class flights for trips under six hours."

def _dumps(value) -> str:
    import json

    return json.dumps(value)


DELETION_TABLES = (
    "conversation_messages, conversations, index_chunks, index_generations, "
    "source_versions, sources, collections, invitations, sessions, users, "
    "workspaces, jobs, outbox"
)


def _run(db: Settings, coro_factory, store_root: Path | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        store = ObjectStore(store_root) if store_root else None
        try:
            return await coro_factory(engine, uow, store)
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

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {DELETION_TABLES} CASCADE"))
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
    """Upload tail: extract → index → activate, as the worker would."""
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
    """Owner, admin, member; a collection; one indexed source with a citation."""

    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            uow = UnitOfWork(engine)
            store = ObjectStore(store_root)
            owner = await create_first_owner(
                uow, EventWriter(), email="owner@example.com", password="owner-password-1"
            )
            base = AccessContext(user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner")
            collection = await create_collection(uow, EventWriter(), context=base, name="Shared")
            admin_invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id,
                email="admin@example.com", role="admin", invited_by=owner.user_id,
            )
            admin = await accept_invitation(
                uow, EventWriter(), token=admin_invite.token, password="admin-password-1"
            )
            member_invite = await issue_invitation_token(
                uow, EventWriter(), workspace_id=owner.workspace_id,
                email="member@example.com", role="member", invited_by=owner.user_id,
            )
            member = await accept_invitation(
                uow, EventWriter(), token=member_invite.token, password="member-password-1"
            )
            accepted = await register_upload(
                uow, EventWriter(), store,
                context=base, collection_id=collection.collection_id,
                name="travel-policy", filename="travel.txt",
                data=DOC_TEXT.encode("utf-8"),
            )
            await _pipeline(uow, store, base, accepted, "travel.txt")
            source_id = accepted.source_id
            version_id = accepted.source_version_id

            # An assistant answer citing the source, plus untouched neighbours.
            conversation_id = str(uuid.uuid4())
            async with uow.transaction() as transaction:
                await transaction.execute(
                    text(
                        "INSERT INTO conversations (id, workspace_id, user_id, title) "
                        "VALUES (:id, :workspace_id, :user_id, 'Travel Q&A')"
                    ),
                    {
                        "id": conversation_id,
                        "workspace_id": owner.workspace_id,
                        "user_id": owner.user_id,
                    },
                )
                await transaction.execute(
                    text(
                        "INSERT INTO conversation_messages (id, conversation_id, role, "
                        "content, citations, language) VALUES (:id, :conversation_id, "
                        "'assistant', :content, :citations, 'en')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "conversation_id": conversation_id,
                        "content": "Economy class is allowed per the travel policy [1].",
                        "citations": f'[{{"label": 1, "chunk_id": "c1", "source_id": '
                                     f'"{source_id}", "source_name": "travel-policy", '
                                     f'"page": null, "block_start": 0, "block_end": 1, '
                                     f'"quote": "economy class flights"}}]',
                    },
                )
                other_citations = [
                    {
                        "label": 1, "chunk_id": "c9",
                        "source_id": "00000000-0000-0000-0000-000000000009",
                        "source_name": "other", "page": None,
                        "block_start": 0, "block_end": 1, "quote": "x",
                    },
                ]
                await transaction.execute(
                    text(
                        "INSERT INTO conversation_messages (id, conversation_id, role, "
                        "content, citations, language) VALUES (:id, :conversation_id, "
                        "'assistant', :content, :citations ::jsonb, 'en')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "conversation_id": conversation_id,
                        "content": "Other topics are covered elsewhere [1].",
                        "citations": _dumps(other_citations),
                    },
                )
                await transaction.execute(
                    text(
                        "INSERT INTO conversation_messages (id, conversation_id, role, "
                        "content) VALUES (:id, :conversation_id, 'user', 'What is allowed?')"
                    ),
                    {"id": str(uuid.uuid4()), "conversation_id": conversation_id},
                )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "admin_id": admin.user_id,
            "member_id": member.user_id,
            "collection_id": collection.collection_id,
            "source_id": source_id,
            "version_id": version_id,
            "conversation_id": conversation_id,
        }

    return asyncio.run(create())


def _owner(workspace: dict) -> AccessContext:
    return AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"], role="owner",
    )


def _admin(workspace: dict) -> AccessContext:
    return AccessContext(
        user_id=workspace["admin_id"], workspace_id=workspace["workspace_id"], role="admin",
    )


def _member(workspace: dict) -> AccessContext:
    return AccessContext(
        user_id=workspace["member_id"], workspace_id=workspace["workspace_id"], role="member",
    )


def test_only_owner_can_request_deletion(db, workspace) -> None:
    def as_admin(_engine, uow, _store) -> None:
        return request_permanent_deletion(
            uow, EventWriter(), context=_admin(workspace), source_id=workspace["source_id"],
        )

    def as_member(_engine, uow, _store) -> None:
        return request_permanent_deletion(
            uow, EventWriter(), context=_member(workspace), source_id=workspace["source_id"],
        )

    with pytest.raises(AccessDenied):
        _run(db, as_admin)
    with pytest.raises(AccessDenied):
        _run(db, as_member)

    async def source_state(_engine, uow, _store) -> str:
        async with uow.transaction() as transaction:
            return str((
                await transaction.execute(
                    text("SELECT state FROM sources WHERE id = :id"),
                    {"id": workspace["source_id"]},
                )
            ).scalar_one())

    assert _run(db, source_state) == "active"  # denials changed nothing


def test_request_tombstones_schedules_and_excludes_in_one_transaction(db, workspace) -> None:
    def request(_engine, uow, _store) -> None:
        return request_permanent_deletion(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )

    _run(db, request)

    async def verify() -> tuple[str, list, list]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                state = (
                    await connection.execute(
                        text("SELECT state FROM sources WHERE id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                pending = (
                    await connection.execute(
                        text(
                            "SELECT topic FROM outbox WHERE published_at IS NULL "
                            "AND topic = 'source_delete'"
                        )
                    )
                ).scalars().all()
                audits = (
                    await connection.execute(
                        text("SELECT type FROM audit_events WHERE type = 'source.delete_requested'")
                    )
                ).scalars().all()
                return str(state), list(pending), list(audits)
        finally:
            await engine.dispose()

    state, pending, audits = asyncio.run(verify())
    assert state == "deleted"
    assert pending == ["source_delete"]  # dispatches only from the committed outbox row
    assert len(audits) == 1

    # Immediate exclusion: no worker needed.
    async def ask() -> list:
        engine = create_async_engine(db.database_url)
        try:
            context = AccessContext(
                user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"],
                role="owner", collection_ids=[workspace["collection_id"]],
            )
            evidence = await retrieve(
                UnitOfWork(engine), FakeEmbeddingProvider(TEST_PROFILE.embedding_dimensions),
                context=context, query="economy class flights", profile=TEST_PROFILE,
            )
            return [item.source_id for item in evidence.items]
        finally:
            await engine.dispose()

    assert asyncio.run(ask()) == []


def test_unknown_or_foreign_source_is_not_found(db, workspace) -> None:
    def foreign(_engine, uow, _store) -> None:
        return request_permanent_deletion(
            uow, EventWriter(), context=_owner(workspace), source_id=str(uuid.uuid4()),
        )

    with pytest.raises(SourceNotFound):
        _run(db, foreign)


def _fake_inventory(source_ids: list[str]) -> InMemoryManagedBackupInventory:
    return InMemoryManagedBackupInventory([
        ManagedBackup(
            id="b1", path="/tmp/b1", created_at=datetime.now(UTC),
            source_ids=frozenset(source_ids),
        ),
    ])


def test_delete_source_purges_redacts_and_records_zero_residue(db, workspace, store_root) -> None:
    def request(_engine, uow, _store) -> None:
        return request_permanent_deletion(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )

    _run(db, request)

    async def purge(engine, uow, store) -> object:
        return await delete_source(
            engine, store, _fake_inventory([workspace["source_id"]]), EventWriter(),
            source_id=workspace["source_id"],
        )

    evidence = _run(db, purge, store_root)
    assert evidence.versions == 1
    assert evidence.manifests == 1
    assert evidence.vectors > 0
    assert evidence.redacted_messages == 1
    assert evidence.backups == 1
    assert evidence.residue == 0

    async def verify() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                sources = (
                    await connection.execute(
                        text("SELECT count(*) FROM sources WHERE id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                versions = (
                    await connection.execute(
                        text("SELECT count(*) FROM source_versions WHERE source_id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                chunks = (
                    await connection.execute(
                        text("SELECT count(*) FROM index_chunks WHERE source_id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                messages = (
                    await connection.execute(
                        text(
                            "SELECT role, content, citations FROM conversation_messages "
                            "ORDER BY number"
                        )
                    )
                ).mappings().all()
                audits = (
                    await connection.execute(
                        text("SELECT metadata FROM audit_events WHERE type = 'source.deleted'")
                    )
                ).scalars().all()
                return {
                    "sources": sources,
                    "versions": versions,
                    "chunks": chunks,
                    "messages": [dict(m) for m in messages],
                    "audits": [str(a) for a in audits],
                }
        finally:
            await engine.dispose()

    final = asyncio.run(verify())
    assert final["sources"] == 0
    assert final["versions"] == 0
    assert final["chunks"] == 0
    # Affected assistant message redacted; the other assistant and user remain.
    assert final["messages"][0]["content"] == DELETION_NOTICE
    assert final["messages"][0]["citations"] == []
    assert final["messages"][1]["content"] == "Other topics are covered elsewhere [1]."
    assert final["messages"][2]["content"] == "What is allowed?"
    assert len(final["audits"]) == 1
    assert "residue" in final["audits"][0] and ": 0" in final["audits"][0]

    # The manifest is gone from the store.
    sha_manifest = store_root / "manifests"
    assert list(sha_manifest.glob("*.json")) == []


def test_delete_source_is_idempotent_on_retry(db, workspace, store_root) -> None:
    def request(_engine, uow, _store) -> None:
        return request_permanent_deletion(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )

    _run(db, request)

    async def purge(engine, uow, store) -> object:
        inventory = _fake_inventory([workspace["source_id"]])
        first = await delete_source(
            engine, store, inventory, EventWriter(), source_id=workspace["source_id"],
        )
        second = await delete_source(
            engine, store, inventory, EventWriter(), source_id=workspace["source_id"],
        )
        return first, second

    first, second = _run(db, purge, store_root)
    assert first.residue == 0
    assert (second.versions, second.manifests, second.vectors) == (0, 0, 0)

    async def audit_count() -> int:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return int((
                    await connection.execute(
                        text("SELECT count(*) FROM audit_events WHERE type = 'source.deleted'")
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(audit_count()) == 1


def test_partial_failure_keeps_tombstone_and_retry_completes(db, workspace, store_root) -> None:
    # A store that explodes on the second delete: the run must abort cleanly.
    class ExplodingStore(ObjectStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.deletes = 0

        def delete(self, address: str) -> None:
            self.deletes += 1
            if self.deletes >= 2:
                raise OSError("simulated purge failure")
            super().delete(address)

    async def failing_pass(engine, uow, store) -> object:
        exploding = ExplodingStore(store_root)
        # Two distinct objects: the replacement must be staged while the
        # source is still live, so it happens before the tombstone request.
        from source_catalog.version_service import stage_replacement

        context = _owner(workspace)
        replacement = await stage_replacement(
            UnitOfWork(engine), EventWriter(), exploding,
            context=context, source_id=workspace["source_id"],
            filename="travel-v2.txt", data=b"Revised policy: book the lowest logical fare.",
        )
        _ = replacement
        await request_permanent_deletion(
            UnitOfWork(engine), EventWriter(), context=context,
            source_id=workspace["source_id"],
        )
        return await delete_source(
            engine, exploding, _fake_inventory([workspace["source_id"]]), EventWriter(),
            source_id=workspace["source_id"],
        )

    with pytest.raises(DeletionIncomplete):
        _run(db, failing_pass, store_root)

    async def tombstone_intact() -> tuple[int, int]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                state = (
                    await connection.execute(
                        text("SELECT state FROM sources WHERE id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                versions = (
                    await connection.execute(
                        text("SELECT count(*) FROM source_versions WHERE source_id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                return str(state), int(versions)
        finally:
            await engine.dispose()

    state, versions = asyncio.run(tombstone_intact())
    assert state == "deleted"
    assert versions == 2  # nothing half-removed

    async def retry(engine, uow, store) -> object:
        return await delete_source(
            engine, store, _fake_inventory([workspace["source_id"]]), EventWriter(),
            source_id=workspace["source_id"],
        )

    evidence = _run(db, retry, store_root)
    assert evidence.residue == 0
    assert evidence.versions == 2


def test_request_is_idempotent_after_tombstone(db, workspace) -> None:
    async def request_twice(_engine, uow, _store) -> None:
        for _ in range(2):
            await request_permanent_deletion(
                uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
            )

    _run(db, request_twice)

    async def counts() -> tuple[int, int]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                audits = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM audit_events "
                            "WHERE type = 'source.delete_requested'"
                        )
                    )
                ).scalar_one()
                pending = (
                    await connection.execute(
                        text("SELECT count(*) FROM outbox WHERE topic = 'source_delete'")
                    )
                ).scalar_one()
                return int(audits), int(pending)
        finally:
            await engine.dispose()

    audits, pending = asyncio.run(counts())
    assert (audits, pending) == (1, 1)  # the second request changed nothing


def test_stuck_backup_fails_verification_and_retry_records_zero_counts(
    db, workspace, store_root,
) -> None:
    """A purge port that cannot remove the backup must abort before any row goes."""

    class StuckInventory(InMemoryManagedBackupInventory):
        def purge_containing(self, source_id: str) -> int:
            return 0  # pretends to purge, removes nothing

    async def attempt(engine, uow, store) -> object:
        await request_permanent_deletion(
            uow, EventWriter(), context=_owner(workspace), source_id=workspace["source_id"],
        )
        return await delete_source(
            engine, store, StuckInventory(
                [ManagedBackup(
                    id="b1", path="/tmp/b1", created_at=datetime.now(UTC),
                    source_ids=frozenset([workspace["source_id"]]),
                )],
            ),
            EventWriter(), source_id=workspace["source_id"],
        )

    with pytest.raises(DeletionIncomplete):
        _run(db, attempt, store_root)

    async def rows_intact() -> tuple[int, int]:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                versions = (
                    await connection.execute(
                        text("SELECT count(*) FROM source_versions WHERE source_id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                chunks = (
                    await connection.execute(
                        text("SELECT count(*) FROM index_chunks WHERE source_id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one()
                return int(versions), int(chunks)
        finally:
            await engine.dispose()

    versions, chunks = asyncio.run(rows_intact())
    assert versions == 1  # catalog rows survive; only the verification failed
    assert chunks == 0  # vectors purge before verification; tombstone hides the gap


def test_actor_composes_settings_store_and_purge(
    db, workspace, store_root, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation.jobs import JobCommand, JobQueue

    async def request_and_enqueue() -> uuid.UUID:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.transaction() as transaction:
                await request_permanent_deletion(
                    uow, EventWriter(), context=_owner(workspace),
                    source_id=workspace["source_id"],
                )
                return await JobQueue().enqueue(
                    JobCommand(
                        type="source_delete", payload={"source_id": workspace["source_id"]},
                    ),
                    transaction,
                )
        finally:
            await engine.dispose()

    job = asyncio.run(request_and_enqueue())

    from apps.worker.tasks import delete_source as task_module

    class FakeSettings:
        database_url = db.database_url
        data_dir = str(store_root)

        @classmethod
        def load(cls) -> "FakeSettings":
            return cls()

    monkeypatch.setattr(task_module, "Settings", FakeSettings)
    monkeypatch.setattr(task_module, "ObjectStore", lambda _root: ObjectStore(store_root))

    result = task_module.source_delete(job_id=str(job), source_id=workspace["source_id"])
    assert result.startswith("deleted:")

    # An incomplete purge fails the lease so dramatiq retries the delivery.
    from source_catalog.deletion import DeletionIncomplete

    async def stuck(*_args, **_kwargs) -> object:
        raise DeletionIncomplete("backup still present")

    monkeypatch.setattr(task_module, "delete_source", stuck)

    async def enqueue_again() -> uuid.UUID:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.transaction() as transaction:
                return await JobQueue().enqueue(
                    JobCommand(
                        type="source_delete", payload={"source_id": workspace["source_id"]},
                    ),
                    transaction,
                )
        finally:
            await engine.dispose()

    retry_job = asyncio.run(enqueue_again())
    retry = task_module.source_delete(job_id=str(retry_job), source_id=workspace["source_id"])
    assert retry.startswith("incomplete:")

    # A delivery whose lease is already settled is skipped, not reprocessed.
    assert task_module.source_delete(job_id=str(job), source_id=workspace["source_id"]) == "skipped"

    async def source_gone() -> int:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return int((
                    await connection.execute(
                        text("SELECT count(*) FROM sources WHERE id = :id"),
                        {"id": workspace["source_id"]},
                    )
                ).scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(source_gone()) == 0


def test_objects_shared_with_other_sources_are_kept(db, workspace, store_root) -> None:
    async def share_and_delete(_engine, uow, store) -> object:
        context = _owner(workspace)
        twin = await register_upload(
            uow, EventWriter(), store,
            context=context, collection_id=workspace["collection_id"],
            name="travel-copy", filename="travel-copy.txt",
            data=DOC_TEXT.encode("utf-8"),  # same bytes → same content address
        )
        _ = twin
        await request_permanent_deletion(
            uow, EventWriter(), context=context, source_id=workspace["source_id"],
        )
        return await delete_source(
            _engine, store, _fake_inventory([workspace["source_id"]]), EventWriter(),
            source_id=workspace["source_id"],
        )

    evidence = _run(db, share_and_delete, store_root)
    assert evidence.manifests == 0  # the object is still referenced by the twin
    assert (store_root / "objects" / DOC_TEXT_SHA).exists()


DOC_TEXT_SHA = hashlib.sha256(DOC_TEXT.encode("utf-8")).hexdigest()
