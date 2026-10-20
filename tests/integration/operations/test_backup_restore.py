"""Task 24a: encrypted backup core, manifest, and the real inventory port."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foundation.config import Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from operations import backup

KEY = "correct horse battery staple"
OTHER_KEY = "wrong passphrase entirely"


@asynccontextmanager
async def _engine(db: Settings):
    engine = create_async_engine(db.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _run(db: Settings, coro_factory):
    async def run():
        async with _engine(db) as engine:
            return await coro_factory(engine)

    return asyncio.run(run())


@pytest.fixture()
def backup_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("SMART_RAG_BACKUP_KEY", KEY)
    return tmp_path / "backups"


@pytest.fixture()
def store(db: Settings, tmp_path: Path) -> ObjectStore:
    return ObjectStore(tmp_path / "data")


def _seed_source(db: Settings, store: ObjectStore, workspace: dict) -> str:
    """One active source with an artifact and an indexed chunk."""
    source_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    sha = store.put(b"PDF body bytes")
    ws = workspace["workspace_id"]
    owner = workspace["owner_id"]

    async def insert(connection) -> None:
        await connection.execute(
            text(
                "INSERT INTO sources (id, workspace_id, collection_id, name, state,"
                " created_by, created_at, updated_at) VALUES "
                "(:id, :ws, :col, 'Handbook.pdf', 'active', :owner, now(), now())"
            ),
            {"id": source_id, "ws": ws, "col": workspace["collection_id"], "owner": owner},
        )
        await connection.execute(
            text(
                "INSERT INTO source_versions (id, source_id, workspace_id, media_type,"
                " filename, object_sha256, size_bytes, state, created_by, created_at,"
                " updated_at) VALUES (:id, :sid, :ws, 'application/pdf', 'h.pdf',"
                " :sha, 15, 'active', :owner, now(), now())"
            ),
            {"id": version_id, "sid": source_id, "ws": ws, "sha": sha, "owner": owner},
        )
        await connection.execute(
            text(
                "INSERT INTO index_generations (id, workspace_id, source_version_id,"
                " compatibility_key, dimension, model, state, chunk_count,"
                " activated_at, created_at) VALUES "
                "(:id, :ws, :vid, 'bge-m3:1024:unigrams', 3, 'bge-m3', 'active', 1,"
                " now(), now())"
            ),
            {"id": generation_id, "ws": ws, "vid": version_id},
        )
        await connection.execute(
            text(
                "INSERT INTO index_chunks (id, generation_id, workspace_id, source_id,"
                " source_version_id, ordinal, text, language, embedding, keywords,"
                " block_start, block_end, created_at) VALUES "
                "(:id, :gid, :ws, :sid, :vid, 0, 'Return policy is 30 days.', 'en',"
                " CAST('[0.1, 0.2, 0.3]' AS vector), to_tsvector('english',"
                " 'return policy 30 days'), 0, 0, now())"
            ),
            {"id": chunk_id, "gid": generation_id, "ws": ws, "sid": source_id,
             "vid": version_id},
        )

    def go(engine: AsyncEngine):
        async def run():
            async with engine.begin() as connection:
                await insert(connection)

        return run()

    _run(db, go)
    return source_id


def _rows(db: Settings, query: str, **params) -> list:
    def read(engine: AsyncEngine):
        async def run():
            async with engine.begin() as connection:
                return (await connection.execute(text(query), params)).mappings().all()

        return run()

    return _run(db, read)


def _make_backup(db: Settings, store: ObjectStore, backup_dir: Path, actor: str = "host"):
    return _run(
        db,
        lambda engine: backup.create_backup(
            engine, store, EventWriter(), backup_dir=backup_dir, actor=actor,
        ),
    )


def test_create_backup_encrypts_and_records(db, workspace, store, backup_env):
    source_id = _seed_source(db, store, workspace)

    record = _make_backup(db, store, backup_env, actor=workspace["owner_id"])
    path = backup_env / record.filename
    assert path.exists()
    row = _rows(db, "SELECT * FROM managed_backups WHERE id = :id", id=str(record.id))[0]
    assert row["filename"] == record.filename
    assert row["sha256"] == backup.sha256_file(path)
    assert row["size_bytes"] == path.stat().st_size
    assert source_id in [str(s) for s in row["source_ids"]]
    events = _rows(db, "SELECT type FROM audit_events WHERE type = 'backup.created'")
    assert len(events) == 1
    # The bundle never holds the recovery key or its derivation material in the clear.
    blob = path.read_bytes()
    assert KEY.encode() not in blob


def test_inspect_backup_needs_no_key(db, workspace, store, backup_env, monkeypatch):
    _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)
    monkeypatch.delenv("SMART_RAG_BACKUP_KEY")
    info = backup.inspect_backup(backup_env / record.filename)
    assert info.filename == record.filename
    assert info.sha256 == record.sha256
    assert info.counts["index_chunks"] == 1
    assert info.counts["users"] == 1
    assert info.schema_revision  # the creating install's alembic revision, e.g. '0014'


def test_wrong_key_or_tampering_fails_authentication(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)
    path = backup_env / record.filename
    with pytest.raises(backup.BackupKeyError):
        _run(db, lambda engine: backup.load_bundle(path, OTHER_KEY))
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0xFF  # tampered ciphertext must not decrypt either
    path.write_bytes(bytes(blob))
    with pytest.raises(backup.BackupKeyError):
        _run(db, lambda engine: backup.load_bundle(path, KEY))


def test_missing_recovery_key_refuses(db, workspace, store, backup_env, monkeypatch):
    monkeypatch.delenv("SMART_RAG_BACKUP_KEY")
    with pytest.raises(backup.BackupKeyError):
        _make_backup(db, store, backup_env)


def test_inventory_lists_and_purges_containing(db, workspace, store, backup_env):
    source_id = _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)

    async def run():
        async with _engine(db) as engine:
            inventory = backup.DbManagedBackupInventory(engine, backup_env)
            found = await inventory.list_containing(source_id)
            assert [item.id for item in found] == [str(record.id)]
            assert found[0].path == str(backup_env / record.filename)
            assert await inventory.purge_containing(source_id) == 1
            assert not (backup_env / record.filename).exists()
            assert await inventory.list_containing(source_id) == []
            assert await inventory.purge_containing(source_id) == 0  # idempotent redelivery

    asyncio.run(run())


def test_deletion_purges_backup_before_verification(db, workspace, store, backup_env):
    """Task 17 acceptance, now with the real inventory: a deleted source's
    backups are gone before the deletion receipt records zero residue."""
    from foundation.unit_of_work import UnitOfWork
    from source_catalog.deletion import delete_source, request_permanent_deletion

    source_id = _seed_source(db, store, workspace)
    _make_backup(db, store, backup_env)

    def purge(engine: AsyncEngine):
        async def go():
            await request_permanent_deletion(
                UnitOfWork(engine), EventWriter(),
                context=workspace_context(workspace), source_id=source_id,
            )
            return await delete_source(
                engine, store, backup.DbManagedBackupInventory(engine, backup_env),
                EventWriter(), source_id=source_id,
            )

        return go()

    evidence = _run(db, purge)
    assert evidence.backups == 1
    assert list(backup_env.iterdir()) == []
    assert _rows(db, "SELECT count(*) AS n FROM sources WHERE id = :id", id=source_id)[0]["n"] == 0


def test_stuck_backup_file_fails_deletion_for_retry(db, workspace, store, backup_env):
    from foundation.unit_of_work import UnitOfWork
    from source_catalog.deletion import (
        DeletionIncomplete,
        delete_source,
        request_permanent_deletion,
    )

    source_id = _seed_source(db, store, workspace)
    _make_backup(db, store, backup_env)

    def stuck(engine: AsyncEngine):
        async def go():
            await request_permanent_deletion(
                UnitOfWork(engine), EventWriter(),
                context=workspace_context(workspace), source_id=source_id,
            )
            return await delete_source(
                engine, store, backup.DbManagedBackupInventory(engine, backup_env),
                EventWriter(), source_id=source_id,
            )

        return go()

    backup_env.chmod(0o555)  # unremovable: the purge must fail, not lie
    try:
        with pytest.raises(DeletionIncomplete):
            _run(db, stuck)
    finally:
        backup_env.chmod(0o755)
    # Row survived with its file; the redelivery reruns the purge.
    assert _rows(db, "SELECT count(*) AS n FROM managed_backups")[0]["n"] == 1


def workspace_context(workspace: dict):
    from identity_access.authorization import AccessContext

    return AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"], role="owner"
    )
