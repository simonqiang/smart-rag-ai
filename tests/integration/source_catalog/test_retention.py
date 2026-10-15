"""Task 17A: retention policy and scheduled purge (live Compose PostgreSQL).

The purge never touches active knowledge: candidates are claimed under a row
lock and rechecked before every destructive step, artifacts are deleted only
when no version row references them and the object predates the cutoff, and
each run records a counts-only audit event.
"""

import asyncio
import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
from source_catalog.retention import (
    RetentionIncomplete,
    get_retention_policy,
    purge_version_if_expired,
    run_retention_purge,
    update_retention_policy,
)

TEST_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)

BODY = "The retention policy keeps superseded knowledge for a bounded period only."

RETENTION_TABLES = (
    "index_chunks, index_generations, source_versions, sources, collections, "
    "conversations, retention_policies, invitations, sessions, users, workspaces, "
    "jobs, outbox"
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
            await connection.execute(text(f"TRUNCATE {RETENTION_TABLES} CASCADE"))
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
    """Extract → index → activate tail of the ingestion pipeline."""
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
    async def create() -> dict:
        engine = create_async_engine(db.database_url)
        try:
            uow = UnitOfWork(engine)
            owner = await create_first_owner(
                uow, EventWriter(), email="owner@example.com", password="owner-password-1"
            )
            owner_context = AccessContext(
                user_id=owner.user_id, workspace_id=owner.workspace_id, role="owner",
                collection_ids=await accessible_collection_ids(
                    uow, user_id=owner.user_id, workspace_id=owner.workspace_id,
                ),
            )
        finally:
            await engine.dispose()
        return {
            "workspace_id": owner.workspace_id,
            "owner_id": owner.user_id,
            "owner_context": owner_context,
        }

    return asyncio.run(create())


def _upload(
    db: Settings, workspace: dict, store_root: Path, *, name: str = "doc",
    state: str = "active", age_days: int | None = None,
) -> str:
    """One indexed version in the requested lifecycle position; returns its id."""

    if "collection_id" not in workspace:
        workspace["collection_id"] = _make_collection(db, workspace)

    async def run(uow, _engine, store) -> str:
        accepted = await register_upload(
            uow, EventWriter(), store,
            context=workspace["owner_context"], collection_id=workspace["collection_id"],
            name=name, filename=f"{name}.txt",
            data=f"{BODY} ({name})".encode("utf-8"),
        )
        return await _pipeline(uow, store, workspace["owner_context"], accepted, f"{name}.txt")

    version_id = str(_run(db, run, store_root))
    _set_version(db, version_id, state, age_days)
    return version_id


def _make_collection(db: Settings, workspace: dict) -> str:
    from source_catalog.catalog import create_collection

    async def run(uow, _engine, _store) -> str:
        created = await create_collection(
            uow, EventWriter(), context=workspace["owner_context"], name="Shared",
        )
        workspace["owner_context"].collection_ids.append(str(created.collection_id))
        return str(created.collection_id)

    return str(_run(db, run))


def _set_version(
    db: Settings, version_id: str, state: str, age_days: int | None = None
) -> None:
    async def run() -> None:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE source_versions SET state = :state, "
                        "updated_at = now() - make_interval(days => :days) WHERE id = :id"
                    ),
                    {"state": state, "days": age_days or 0, "id": version_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _counts(db: Settings, query: str, params: dict | None = None) -> int:
    async def run() -> int:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return int((
                    await connection.execute(text(query), params or {})
                ).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _scalar(db: Settings, query: str, params: dict) -> str:
    async def run() -> str:
        engine = create_async_engine(db.database_url)
        try:
            async with engine.begin() as connection:
                return str((await connection.execute(text(query), params)).scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _age_artifacts(store_root: Path, days: int = 40) -> None:
    stale = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    for directory in ("objects", "manifests"):
        for path in (store_root / directory).iterdir():
            os.utime(path, (stale, stale))


def _purge(db: Settings, store_root: Path, store: ObjectStore | None = None) -> dict:
    return asdict(_run(
        db,
        lambda uow, engine, _store: run_retention_purge(
            engine, store or ObjectStore(store_root), EventWriter(),
        ),
        store_root,
    ))


def test_policy_defaults_when_no_row_exists(db, workspace) -> None:
    async def run(uow, _engine, _store) -> None:
        policy = await get_retention_policy(uow, workspace_id=workspace["workspace_id"])
        assert policy.superseded_days == 30
        assert policy.conversation_days == 90

    _run(db, run)


def test_owner_updates_policy_and_audit_is_recorded(db, workspace) -> None:
    async def run(uow, _engine, _store) -> None:
        policy = await update_retention_policy(
            uow, EventWriter(), context=workspace["owner_context"],
            superseded_days=7, conversation_days=14,
        )
        assert (policy.superseded_days, policy.conversation_days) == (7, 14)
        # Upsert: a second update replaces the row instead of failing.
        again = await update_retention_policy(
            uow, EventWriter(), context=workspace["owner_context"],
            superseded_days=8, conversation_days=15,
        )
        assert (again.superseded_days, again.conversation_days) == (8, 15)

    _run(db, run)
    updates = _counts(
        db, "SELECT count(*) FROM audit_events WHERE type = 'retention.updated'"
    )
    assert updates == 2


def test_non_owner_cannot_update_policy(db, workspace) -> None:
    admins = [
        AccessContext(
            user_id=str(uuid.uuid4()), workspace_id=workspace["workspace_id"], role=role,
        )
        for role in ("admin", "member")
    ]

    async def run(uow, _engine, _store) -> None:
        for context in admins:
            try:
                await update_retention_policy(
                    uow, EventWriter(), context=context,
                    superseded_days=7, conversation_days=14,
                )
            except AccessDenied:
                continue
            raise AssertionError(f"{context.role} updated the retention policy")

    _run(db, run)
    assert _counts(db, "SELECT count(*) FROM retention_policies") == 0


def test_policy_rejects_non_positive_days(db, workspace) -> None:
    async def run(uow, _engine, _store) -> None:
        for days in (0, -3):
            try:
                await update_retention_policy(
                    uow, EventWriter(), context=workspace["owner_context"],
                    superseded_days=days, conversation_days=90,
                )
            except ValueError:
                continue
            raise AssertionError(f"accepted superseded_days={days}")

    _run(db, run)


def test_expired_superseded_version_is_purged_with_vectors(db, workspace, store_root) -> None:
    old = _upload(db, workspace, store_root, name="old", state="superseded", age_days=40)
    active = _upload(db, workspace, store_root, name="new", state="active")

    chunks_before = _counts(
        db, "SELECT count(*) FROM index_chunks WHERE source_version_id = :id", {"id": old},
    )
    assert chunks_before > 0

    evidence = _purge(db, store_root)

    assert evidence["versions"] == 1
    assert _counts(db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": old}) == 0
    assert _counts(
        db, "SELECT count(*) FROM index_chunks WHERE source_version_id = :id", {"id": old},
    ) == 0
    assert _counts(
        db, "SELECT count(*) FROM index_generations WHERE source_version_id = :id", {"id": old},
    ) == 0
    # Active knowledge untouched.
    assert _counts(db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": active}) == 1
    assert _counts(
        db, "SELECT count(*) FROM index_chunks WHERE source_version_id = :id", {"id": active},
    ) == chunks_before

    # Once the freed artifact ages past the cutoff, the scan clears it — and
    # that second run is otherwise a no-op (idempotent redelivery).
    _age_artifacts(store_root)
    second = _purge(db, store_root)
    assert second["versions"] == 0
    assert second["artifacts"] == 1


def test_retired_page_versions_are_purged(db, workspace, store_root) -> None:
    # Task 20 website snapshots retire pages into this state; the purge path
    # is the version lifecycle's, not a special case.
    page = _upload(db, workspace, store_root, name="page", state="retired", age_days=40)
    fresh = _upload(db, workspace, store_root, name="live", state="retired", age_days=1)

    evidence = _purge(db, store_root)

    assert evidence["versions"] == 1
    assert _counts(db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": page}) == 0
    assert _counts(db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": fresh}) == 1


def test_unreferenced_aged_artifacts_are_purged_but_referenced_survive(
    db, workspace, store_root,
) -> None:
    store = ObjectStore(store_root)
    orphan = store.put(b"an artifact no version row references")
    shared = store.put(b"content shared with a version row")
    _age_artifacts(store_root)
    # A version row references the shared artifact (rollback-reuse shape).
    seed = _upload(db, workspace, store_root, name="seed", state="active")
    source_id = _scalar(
        db, "SELECT source_id FROM source_versions WHERE id = :id", {"id": seed},
    )

    async def link() -> None:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO source_versions (id, workspace_id, source_id, object_sha256, "
                        "size_bytes, media_type, filename, state, created_by) "
                        "SELECT :id, workspace_id, :source, :sha, size_bytes, media_type, "
                        "filename, 'superseded', created_by FROM source_versions WHERE id = :seed"
                    ),
                    {
                        "id": str(uuid.uuid4()), "source": source_id,
                        "sha": shared, "seed": seed,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(link())
    evidence = _purge(db, store_root)

    assert evidence["artifacts"] == 1  # the orphan only
    assert not (store_root / "objects" / orphan).exists()
    assert (store_root / "objects" / shared).exists()


def test_fresh_unreferenced_artifacts_are_never_purged(db, workspace, store_root) -> None:
    store = ObjectStore(store_root)
    _upload(db, workspace, store_root, name="seed", state="active")
    fresh = store.put(b"just written; a racing upload may still owe its row")

    evidence = _purge(db, store_root)

    assert (store_root / "objects" / fresh).exists()
    assert evidence["artifacts"] == 0


def test_expired_conversations_are_purged(db, workspace) -> None:
    async def seed() -> None:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO conversations (id, workspace_id, user_id, title, updated_at) "
                        "VALUES (:id, :ws, :user, 'stale', now() - interval '120 days'), "
                        "(:fresh, :ws, :user, 'current', now())"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "fresh": str(uuid.uuid4()),
                        "ws": workspace["workspace_id"],
                        "user": workspace["owner_id"],
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())

    evidence = _purge(db, Path(str(db.database_url) + "-unused"))

    assert evidence["conversations"] == 1
    assert _counts(db, "SELECT count(*) FROM conversations WHERE title = 'stale'") == 0
    assert _counts(db, "SELECT count(*) FROM conversations WHERE title = 'current'") == 1


def test_active_indexed_and_uploaded_versions_are_never_purged(
    db, workspace, store_root,
) -> None:
    active = _upload(db, workspace, store_root, name="active", state="active", age_days=400)
    indexed = _upload(db, workspace, store_root, name="indexed", state="indexed", age_days=400)
    uploaded = _upload(db, workspace, store_root, name="uploaded", state="uploaded", age_days=400)

    _purge(db, store_root)

    for version_id in (active, indexed, uploaded):
        assert _counts(
            db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": version_id},
        ) == 1
    assert _counts(
        db, "SELECT count(*) FROM index_chunks WHERE source_version_id = :id", {"id": active},
    ) > 0


def test_state_change_after_the_snapshot_releases_the_candidate(
    db, workspace, store_root,
) -> None:
    """A row that left the purgeable window before its destructive step is
    released without deleting anything."""
    stale_state = _upload(db, workspace, store_root, name="old", state="superseded", age_days=40)
    young = _upload(db, workspace, store_root, name="young", state="superseded", age_days=1)
    _upload(db, workspace, store_root, name="new", state="active")
    # The race: the row left the candidate set after enumeration.
    _set_version(db, stale_state, "active")

    cutoff = datetime.now(UTC) - timedelta(days=30)

    async def run(version_id: str) -> bool:
        engine = create_async_engine(Settings.load().database_url)
        try:
            return await purge_version_if_expired(engine, version_id, cutoff)
        finally:
            await engine.dispose()

    assert asyncio.run(run(stale_state)) is False  # state changed: release
    assert asyncio.run(run(young)) is False  # younger than the cutoff: release
    for version_id in (stale_state, young):
        assert _counts(
            db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": version_id},
        ) == 1


def test_purge_versus_rollback_shares_the_artifact(db, workspace, store_root) -> None:
    """Rollback inserts a new version row for the same content-addressed
    object; purging the expired version must keep that object."""
    old = _upload(db, workspace, store_root, name="old", state="superseded", age_days=40)
    sha = _scalar(
        db, "SELECT object_sha256 FROM source_versions WHERE id = :id", {"id": old},
    )
    source_id = _scalar(
        db, "SELECT source_id FROM source_versions WHERE id = :id", {"id": old},
    )

    async def link() -> None:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO source_versions (id, workspace_id, source_id, object_sha256, "
                        "size_bytes, media_type, filename, state, created_by) "
                        "SELECT :id, workspace_id, :source, object_sha256, size_bytes, media_type, "
                        "filename, 'uploaded', created_by FROM source_versions WHERE id = :old"
                    ),
                    {"id": str(uuid.uuid4()), "source": source_id, "old": old},
                )
        finally:
            await engine.dispose()

    asyncio.run(link())
    _purge(db, store_root)

    assert _counts(
        db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": old},
    ) == 0
    assert (store_root / "objects" / sha).exists()


def test_shortened_retention_purges_sooner(db, workspace, store_root) -> None:
    old = _upload(db, workspace, store_root, name="old", state="superseded", age_days=2)
    _upload(db, workspace, store_root, name="new", state="active")

    _purge(db, store_root)
    assert _counts(
        db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": old},
    ) == 1  # the 30-day default keeps it

    async def shorten() -> None:
        engine = create_async_engine(Settings.load().database_url)
        try:
            await update_retention_policy(
                UnitOfWork(engine), EventWriter(), context=workspace["owner_context"],
                superseded_days=1, conversation_days=90,
            )
        finally:
            await engine.dispose()

    asyncio.run(shorten())
    evidence = _purge(db, store_root)
    assert evidence["versions"] == 1
    assert _counts(
        db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": old},
    ) == 0


def test_artifact_failure_retries_cleanly(db, workspace, store_root) -> None:
    """A failing artifact deletion raises RetentionIncomplete after the row
    deletions commit; the retry finishes the scan and nothing else moves."""

    class ExplodingStore(ObjectStore):
        def delete(self, address: str) -> None:
            if address == sha:
                raise OSError("simulated purge failure")
            super().delete(address)

    victim = _upload(db, workspace, store_root, name="old", state="superseded", age_days=40)
    sha = _scalar(
        db, "SELECT object_sha256 FROM source_versions WHERE id = :id", {"id": victim},
    )
    _upload(db, workspace, store_root, name="new", state="active")
    _age_artifacts(store_root)

    with pytest.raises(RetentionIncomplete):
        _purge(db, store_root, store=ExplodingStore(store_root))
    # Version rows are already gone; only the artifact is stuck.
    assert _counts(
        db,
        "SELECT count(*) FROM source_versions WHERE state = 'superseded' AND id = :id",
        {"id": victim},
    ) == 0
    assert (store_root / "objects" / sha).exists()

    evidence = _purge(db, store_root)
    assert evidence["versions"] == 0
    assert evidence["artifacts"] == 1
    assert not (store_root / "objects" / sha).exists()


def test_counts_only_audit_event_per_run(db, workspace, store_root) -> None:
    _upload(db, workspace, store_root, name="old", state="superseded", age_days=40)
    _upload(db, workspace, store_root, name="new", state="active")

    _purge(db, store_root)

    async def read() -> list[dict]:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT type, actor, subject, metadata FROM audit_events "
                            "WHERE type = 'retention.purged'"
                        )
                    )
                ).mappings().all()
        finally:
            await engine.dispose()
        return [dict(row) for row in rows]

    events = asyncio.run(read())
    assert len(events) == 1
    event = events[0]
    assert str(event["actor"]) == "system"
    assert str(event["subject"]) == workspace["workspace_id"]
    assert set(event["metadata"].keys()) == {
        "versions", "artifacts", "conversations", "workspace_id",
    }


def test_run_on_empty_database_is_a_noop(db, tmp_path: Path) -> None:
    evidence = _purge(db, tmp_path / "store")
    assert evidence == {"versions": 0, "artifacts": 0, "conversations": 0}


def test_worker_actor_claims_and_completes(db, workspace, store_root, monkeypatch) -> None:
    from apps.worker.tasks import retention_purge as task_module
    from foundation.jobs import JobCommand, JobQueue

    victim = _upload(db, workspace, store_root, name="old", state="superseded", age_days=40)
    _upload(db, workspace, store_root, name="new", state="active")
    _age_artifacts(store_root)

    async def seed() -> str:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                return str(await JobQueue().enqueue(
                    JobCommand(
                        type="retention_purge", payload={},
                        idempotency_key="retention_purge:test-day",
                    ),
                    connection,
                ))
        finally:
            await engine.dispose()

    job_id = asyncio.run(seed())
    monkeypatch.setattr(task_module, "ObjectStore", lambda _root: ObjectStore(store_root))

    assert task_module.retention_purge(job_id=job_id) == "purged:1"
    assert _counts(
        db, "SELECT count(*) FROM source_versions WHERE id = :id", {"id": victim},
    ) == 0
    assert _counts(
        db, "SELECT count(*) FROM jobs WHERE id = :id AND status = 'completed'", {"id": job_id},
    ) == 1


def test_worker_actor_retries_when_artifact_purge_fails(
    db, workspace, store_root, monkeypatch,
) -> None:
    from apps.worker.tasks import retention_purge as task_module
    from foundation.jobs import JobCommand, JobQueue
    from source_catalog.retention import RetentionIncomplete

    async def seed() -> str:
        engine = create_async_engine(Settings.load().database_url)
        try:
            async with engine.begin() as connection:
                return str(await JobQueue().enqueue(
                    JobCommand(
                        type="retention_purge", payload={},
                        idempotency_key="retention_purge:test-retry",
                    ),
                    connection,
                ))
        finally:
            await engine.dispose()

    job_id = asyncio.run(seed())

    async def stuck(engine, store, writer):
        raise RetentionIncomplete("simulated stuck artifact")

    monkeypatch.setattr(task_module, "run_retention_purge", stuck)
    with pytest.raises(RetentionIncomplete):
        task_module.retention_purge(job_id=job_id)
    # The lease was released back to pending for the Dramatiq redelivery.
    assert _counts(
        db, "SELECT count(*) FROM jobs WHERE id = :id AND status = 'pending'", {"id": job_id},
    ) == 1


def test_daily_schedule_enqueues_once_per_day(db) -> None:
    from apps.worker.relay import schedule_daily_retention_purge

    async def run() -> str:
        engine = create_async_engine(Settings.load().database_url)
        try:
            first = await schedule_daily_retention_purge(engine)
            second = await schedule_daily_retention_purge(engine)
            return f"{first}:{second}"
        finally:
            await engine.dispose()

    first, second = asyncio.run(run()).split(":")
    assert first == second
    assert _counts(
        db, "SELECT count(*) FROM jobs WHERE type = 'retention_purge'",
    ) == 1
    assert _counts(
        db, "SELECT count(*) FROM outbox WHERE topic = 'retention_purge'",
    ) == 1
