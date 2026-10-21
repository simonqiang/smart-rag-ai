"""Task 24a: encrypted backup core, manifest, and the real inventory port."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from foundation.config import ModelProfile, Settings
from foundation.events import EventWriter
from foundation.storage import ObjectStore
from operations import backup

KEY = "correct horse battery staple"
OTHER_KEY = "wrong passphrase entirely"
SMOKE_PROFILE = ModelProfile(
    name="test", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=3,
)


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
    directory = tmp_path / "backups"
    directory.mkdir()
    return directory


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


ALL_TABLES = (
    "index_chunks, index_generations, source_versions, site_sync_schedules, sources, "
    "collection_grants, collections, conversation_messages, conversations, "
    "retention_policies, generation_provider_settings, invitations, sessions, "
    "users, workspaces, jobs, outbox, managed_backups"
)


def _truncate_all(db: Settings) -> None:
    async def run():
        engine = create_async_engine(db.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {ALL_TABLES} CASCADE"))
            await connection.execute(text("TRUNCATE audit_events"))
        await engine.dispose()

    asyncio.run(run())


def _craft_backup(db: Settings, store: ObjectStore, backup_env: Path, *,
                  revision: str | None = None, drop_objects: bool = False,
                  drop_table: str | None = None, poison_object: bool = False) -> Path:
    """A valid envelope whose payload was assembled outside create_backup, so
    revision/object-member damage can be exercised without breaking the GCM
    authentication the way byte tampering would."""
    import hashlib
    import io
    import json
    import os
    import zipfile

    tables, head_revision, source_ids = _run(db, lambda engine: backup._dump(engine))
    bundle = backup._bundle(tables, revision or head_revision, "2026-10-21T00:00:00+00:00", store)
    if drop_objects or drop_table or poison_object:
        rewritten = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(bundle)) as src, \
                zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as dst:
            manifest = json.loads(src.read("manifest.json"))
            members = dict(manifest["members"])
            for name in src.namelist():
                data = src.read(name)
                if drop_objects and name.startswith("objects/"):
                    continue  # manifest still lists it: verification must catch the gap
                if drop_table and name == f"tables/{drop_table}.jsonl":
                    members.pop(name)
                    continue
                if poison_object and name.startswith("objects/"):
                    # checksummed content under an address it does not hash to
                    forged = "objects/" + "0" * 64
                    members.pop(name)
                    members[forged] = hashlib.sha256(data).hexdigest()
                    dst.writestr(forged, data)
                    continue
                dst.writestr(name, data)
            manifest["members"] = members
            dst.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            )
        bundle = rewritten.getvalue()
    header = {
        "format": backup.BUNDLE_FORMAT,
        "format_version": backup.FORMAT_VERSION,
        "created_at": "2026-10-21T00:00:00+00:00",
        "schema_revision": revision or head_revision,
        "kdf_salt": os.urandom(16).hex(),
        "nonce": os.urandom(12).hex(),
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "counts": {table: len(rows) for table, rows in tables.items()},
        "source_ids": source_ids,
    }
    path = backup_env / f"craft-{revision or 'head'}{'-noobj' if drop_objects else ''}.srb"
    path.write_bytes(backup._encrypt(bundle, header))
    return path


def _restore(db: Settings, store: ObjectStore, path: Path, key: str = KEY):
    return _run(
        db,
        lambda engine: backup.restore_backup(
            engine, store, EventWriter(), path=path, recovery_key=key, actor="host:simon",
        ),
    )


def test_restore_recovers_clean_install_and_retrieval(db, workspace, store, backup_env):
    from ai_providers.fakes import FakeEmbeddingProvider
    from foundation.unit_of_work import UnitOfWork
    from identity_access.authorization import AccessContext
    from retrieval_answering.retrieval import retrieve

    source_id = _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)
    _truncate_all(db)
    assert _rows(db, "SELECT count(*) AS n FROM users")[0]["n"] == 0

    result = _restore(db, store, backup_env / record.filename)
    assert result.filename == record.filename
    assert result.rows > 0
    assert result.sources == 1
    assert _rows(db, "SELECT count(*) AS n FROM users")[0]["n"] == 1
    assert _rows(db, "SELECT count(*) AS n FROM sources WHERE id = :id", id=source_id)[0]["n"] == 1
    chunk = _rows(db, "SELECT text, embedding IS NOT NULL AS has FROM index_chunks")[0]
    assert chunk["text"] == "Return policy is 30 days."
    assert chunk["has"]
    events = _rows(db, "SELECT metadata FROM audit_events WHERE type = 'backup.restored'")
    assert len(events) == 1
    assert events[0]["metadata"]["sources"] == 1

    async def smoke():
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            return await retrieve(
                uow, FakeEmbeddingProvider(3),
                context=AccessContext(
                    user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"],
                    role="owner", collection_ids=[workspace["collection_id"]],
                ),
                query="return policy", profile=SMOKE_PROFILE,
            )
        finally:
            await engine.dispose()

    hits = asyncio.run(smoke())
    assert hits.items, "restored chunks must stay retrievable"
    assert hits.items[0].source_name == "Handbook.pdf"


def test_restore_refuses_wrong_key_and_leaves_target_clean(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)
    _truncate_all(db)
    with pytest.raises(backup.BackupKeyError):
        _restore(db, store, backup_env / record.filename, key=OTHER_KEY)
    assert _rows(db, "SELECT count(*) AS n FROM users")[0]["n"] == 0
    assert _rows(db, "SELECT count(*) AS n FROM audit_events WHERE type = 'backup.restored'")[0]["n"] == 0


def test_restore_refuses_unreadable_file(db, workspace, store, backup_env):
    _truncate_all(db)
    junk = backup_env / "junk.srb"
    junk.write_bytes(b"not a backup at all")
    with pytest.raises(backup.BackupCorrupt):
        _restore(db, store, junk)


def test_restore_refuses_foreign_schema_revision(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    path = _craft_backup(db, store, backup_env, revision="0001")
    _truncate_all(db)
    with pytest.raises(backup.BackupIncompatible):
        _restore(db, store, path)
    assert _rows(db, "SELECT count(*) AS n FROM users")[0]["n"] == 0


def test_restore_refuses_missing_object_member(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    path = _craft_backup(db, store, backup_env, drop_objects=True)
    _truncate_all(db)
    with pytest.raises(backup.BackupCorrupt):
        _restore(db, store, path)


def test_restore_refuses_object_address_mismatch(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    path = _craft_backup(db, store, backup_env, poison_object=True)
    _truncate_all(db)
    with pytest.raises(backup.BackupCorrupt, match="does not match its address"):
        _restore(db, store, path)


def test_restore_refuses_missing_table_member(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    path = _craft_backup(db, store, backup_env, drop_table="users")
    _truncate_all(db)
    with pytest.raises(backup.BackupCorrupt, match="missing table users"):
        _restore(db, store, path)
    assert _rows(db, "SELECT count(*) AS n FROM workspaces")[0]["n"] == 0


def test_restore_refuses_unmigrated_target(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)
    _truncate_all(db)

    async def rename(table_from: str, table_to: str) -> None:
        engine = create_async_engine(db.database_url)
        async with engine.begin() as connection:
            await connection.execute(text(f"ALTER TABLE {table_from} RENAME TO {table_to}"))
        await engine.dispose()

    asyncio.run(rename("alembic_version", "alembic_version_hidden"))
    try:
        with pytest.raises(backup.BackupIncompatible, match="no schema"):
            _restore(db, store, backup_env / record.filename)
    finally:
        asyncio.run(rename("alembic_version_hidden", "alembic_version"))


def test_restore_refuses_occupied_target(db, workspace, store, backup_env):
    _seed_source(db, store, workspace)
    record = _make_backup(db, store, backup_env)
    with pytest.raises(backup.TargetNotEmpty):
        _restore(db, store, backup_env / record.filename)
    assert _rows(db, "SELECT count(*) AS n FROM users WHERE email = 'owner@example.com'")[0]["n"] == 1


def workspace_context(workspace: dict):
    from identity_access.authorization import AccessContext

    return AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"], role="owner"
    )
