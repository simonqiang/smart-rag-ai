"""Task 5: persistence, jobs, storage, audit/outbox, backup-inventory ports.

Runs against the live Compose PostgreSQL and Redis (loopback ports). Every
test is deterministic: state is truncated before each test, Redis is flushed,
and each test drives exactly one event loop (engines are loop-bound).
"""

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.events import AuditEvent, EventWriter, OutboxEvent
from foundation.jobs import (
    AlreadyClaimed,
    JobClaims,
    JobCommand,
    JobDispatcher,
    JobQueue,
    Lease,
)
from foundation.storage import (
    InMemoryManagedBackupInventory,
    ManagedBackup,
    ObjectStore,
)
from foundation.unit_of_work import UnitOfWork

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="module")
def _migrated(settings: Settings):
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@pytest.fixture()
def db(_migrated, settings: Settings) -> Settings:
    async def reset() -> None:
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE jobs, outbox, audit_events"))
        await engine.dispose()

    asyncio.run(reset())
    return settings


# --- unit of work + event writer ---------------------------------------------


def test_committed_transaction_persists_audit_and_outbox(db: Settings) -> None:
    async def scenario() -> tuple[int, int]:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        writer = EventWriter()
        try:
            async with uow.transaction() as transaction:
                await writer.record(
                    AuditEvent(type="source.created", actor="member-1", subject="source-9"),
                    transaction,
                )
                await writer.record(
                    OutboxEvent(topic="index.rebuild", payload={"v": 1}), transaction
                )

            async with engine.connect() as connection:
                audits = (
                    await connection.execute(text("SELECT count(*) FROM audit_events"))
                ).scalar_one()
                outbox = (
                    await connection.execute(text("SELECT count(*) FROM outbox"))
                ).scalar_one()
            return audits, outbox
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (1, 1)


def test_rolled_back_transaction_persists_nothing(db: Settings) -> None:
    async def scenario() -> int:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        writer = EventWriter()
        try:
            with pytest.raises(RuntimeError):
                async with uow.transaction() as transaction:
                    await writer.record(AuditEvent(type="x", actor="a", subject="s"), transaction)
                    raise RuntimeError("boom")

            async with engine.connect() as connection:
                return (
                    await connection.execute(text("SELECT count(*) FROM audit_events"))
                ).scalar_one()
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == 0


# --- job queue ---------------------------------------------------------------


async def _enqueue(engine, key: str | None = "k1", job_type: str = "extract_document") -> uuid.UUID:
    queue = JobQueue()
    async with UnitOfWork(engine).transaction() as transaction:
        return await queue.enqueue(
            JobCommand(type=job_type, payload={"source_id": "s1"}, idempotency_key=key),
            transaction,
        )


def test_enqueue_writes_job_and_outbox_in_caller_transaction(db: Settings) -> None:
    async def scenario() -> tuple[str, int]:
        engine = create_async_engine(db.database_url)
        try:
            job_id = await _enqueue(engine)
            async with engine.connect() as connection:
                status = (
                    await connection.execute(
                        text("SELECT status FROM jobs WHERE id = CAST(:id AS uuid)"),
                        {"id": str(job_id)},
                    )
                ).scalar_one()
                outbox_rows = (
                    await connection.execute(text("SELECT count(*) FROM outbox"))
                ).scalar_one()
            return status, outbox_rows
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == ("pending", 1)


def test_enqueue_is_idempotent_on_key(db: Settings) -> None:
    async def scenario() -> tuple[uuid.UUID, uuid.UUID, int, int]:
        engine = create_async_engine(db.database_url)
        try:
            first = await _enqueue(engine, key="same")
            second = await _enqueue(engine, key="same")
            async with engine.connect() as connection:
                jobs = (await connection.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
                outbox_rows = (
                    await connection.execute(text("SELECT count(*) FROM outbox"))
                ).scalar_one()
            return first, second, jobs, outbox_rows
        finally:
            await engine.dispose()

    first, second, jobs, outbox_rows = asyncio.run(scenario())
    assert first == second
    assert (jobs, outbox_rows) == (1, 1)


def test_rolled_back_enqueue_never_reaches_redis(
    db: Settings, settings: Settings
) -> None:
    async def scenario() -> int:
        import dramatiq
        from dramatiq.brokers.redis import RedisBroker
        from redis.asyncio import Redis as AsyncRedis

        broker = RedisBroker(url=settings.redis_url)
        dramatiq.set_broker(broker)
        client = AsyncRedis.from_url(settings.redis_url)
        engine = create_async_engine(db.database_url)
        try:
            await client.flushdb()
            queue = JobQueue()
            with pytest.raises(RuntimeError):
                async with UnitOfWork(engine).transaction() as transaction:
                    await queue.enqueue(
                        JobCommand(type="extract_document", payload={}), transaction
                    )
                    raise RuntimeError("rollback")
            await JobDispatcher(engine, broker).relay()
            return await client.llen("dramatiq:default")
        finally:
            await engine.dispose()
            await client.flushdb()  # shared stack Redis: drop test messages
            await client.aclose()

    assert asyncio.run(scenario()) == 0


def test_dispatcher_relays_committed_outbox_to_broker(db: Settings, settings: Settings) -> None:
    async def scenario() -> tuple[int, int]:
        import dramatiq
        from dramatiq.brokers.redis import RedisBroker
        from redis.asyncio import Redis as AsyncRedis

        broker = RedisBroker(url=settings.redis_url)
        dramatiq.set_broker(broker)
        client = AsyncRedis.from_url(settings.redis_url)
        engine = create_async_engine(db.database_url)
        try:
            await client.flushdb()
            await _enqueue(engine)
            # A scalar jsonb payload decodes to a str; the dispatcher must cope.
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO outbox (id, topic, payload) "
                        "VALUES (gen_random_uuid(), 'scalar_topic', '\"text-payload\"')"
                    )
                )
            sent = await JobDispatcher(engine, broker).relay()
            return sent, await client.llen("dramatiq:default")
        finally:
            await engine.dispose()
            await client.flushdb()  # shared stack Redis: drop test messages
            await client.aclose()

    assert asyncio.run(scenario()) == (2, 2)


def test_crash_before_publication_mark_republishes(db: Settings, settings: Settings) -> None:
    """At-least-once: a crash between broker send and the publication mark
    causes a duplicate relay; consumers deduplicate through job claims."""

    async def scenario() -> int:
        import dramatiq
        from dramatiq.brokers.redis import RedisBroker
        from redis.asyncio import Redis as AsyncRedis

        broker = RedisBroker(url=settings.redis_url)
        dramatiq.set_broker(broker)
        client = AsyncRedis.from_url(settings.redis_url)
        engine = create_async_engine(db.database_url)
        try:
            await client.flushdb()
            await _enqueue(engine)
            dispatcher = JobDispatcher(engine, broker)
            await dispatcher.relay()
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE outbox SET published_at = NULL"))
            await dispatcher.relay()
            return await client.llen("dramatiq:default")
        finally:
            await engine.dispose()
            await client.flushdb()  # shared stack Redis: drop test messages
            await client.aclose()

    assert asyncio.run(scenario()) == 2  # duplicate delivered; claims deduplicate


def test_dispatcher_republishes_after_redis_loss(db: Settings, settings: Settings) -> None:
    async def scenario() -> int:
        import dramatiq
        from dramatiq.brokers.redis import RedisBroker
        from redis.asyncio import Redis as AsyncRedis

        broker = RedisBroker(url=settings.redis_url)
        dramatiq.set_broker(broker)
        client = AsyncRedis.from_url(settings.redis_url)
        engine = create_async_engine(db.database_url)
        try:
            await client.flushdb()
            await _enqueue(engine)
            await JobDispatcher(engine, broker).relay()
            await client.flushdb()  # Redis loses the message

            async with engine.begin() as connection:
                await connection.execute(text("UPDATE outbox SET published_at = NULL"))
            await JobDispatcher(engine, broker).relay()
            return await client.llen("dramatiq:default")
        finally:
            await engine.dispose()
            await client.flushdb()  # shared stack Redis: drop test messages
            await client.aclose()

    assert asyncio.run(scenario()) == 1


# --- job claims and leases ----------------------------------------------------


def test_second_claim_of_running_job_is_rejected(db: Settings) -> None:
    async def scenario() -> tuple[bool, bool]:
        engine = create_async_engine(db.database_url)
        claims = JobClaims(engine)
        try:
            job_id = await _enqueue(engine)
            first = await claims.claim(job_id, lease_seconds=30)
            second = await claims.claim(job_id, lease_seconds=30)
            assert repr(second) == "AlreadyClaimed"
            return isinstance(first, Lease), second is AlreadyClaimed
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (True, True)


def test_completed_job_cannot_be_claimed(db: Settings) -> None:
    async def scenario() -> bool:
        engine = create_async_engine(db.database_url)
        claims = JobClaims(engine)
        try:
            job_id = await _enqueue(engine)
            lease = await claims.claim(job_id, lease_seconds=30)
            assert isinstance(lease, Lease)
            await claims.complete(lease)
            return await claims.claim(job_id, lease_seconds=30) is AlreadyClaimed
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) is True


def test_expired_lease_can_be_reclaimed_with_new_token(db: Settings) -> None:
    async def scenario() -> tuple[bool, bool]:
        engine = create_async_engine(db.database_url)
        claims = JobClaims(engine)
        try:
            job_id = await _enqueue(engine)
            first = await claims.claim(job_id, lease_seconds=30)
            assert isinstance(first, Lease)
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = CAST(:id AS uuid)"),
                    {"id": str(job_id)},
                )
            second = await claims.claim(job_id, lease_seconds=30)
            return isinstance(second, Lease), isinstance(second, Lease) and second.token != first.token
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (True, True)


def test_completion_requires_valid_lease_token(db: Settings) -> None:
    async def scenario() -> tuple[bool, str, bool]:
        engine = create_async_engine(db.database_url)
        claims = JobClaims(engine)
        try:
            job_id = await _enqueue(engine)
            lease = await claims.claim(job_id, lease_seconds=30)
            assert isinstance(lease, Lease)
            stale = Lease(job_id=lease.job_id, token="stale-token", expires_at=lease.expires_at)

            wrong_token = await claims.complete(stale)
            async with engine.connect() as connection:
                status = (
                    await connection.execute(
                        text("SELECT status FROM jobs WHERE id = CAST(:id AS uuid)"),
                        {"id": str(job_id)},
                    )
                ).scalar_one()
            right_token = await claims.complete(lease)
            return wrong_token, status, right_token
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (False, "running", True)


def test_renew_extends_lease_only_for_the_holding_token(db: Settings) -> None:
    async def scenario() -> tuple[bool, bool]:
        engine = create_async_engine(db.database_url)
        claims = JobClaims(engine)
        try:
            job_id = await _enqueue(engine)
            lease = await claims.claim(job_id, lease_seconds=30)
            assert isinstance(lease, Lease)
            stale = Lease(job_id=lease.job_id, token="stale-token", expires_at=lease.expires_at)

            renewed = await claims.renew(lease, lease_seconds=120)
            rejected = await claims.renew(stale)
            return renewed, rejected
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (True, False)


def test_failed_job_is_terminal_and_reports_token_validity(db: Settings) -> None:
    async def scenario() -> tuple[bool, str, bool]:
        engine = create_async_engine(db.database_url)
        claims = JobClaims(engine)
        try:
            job_id = await _enqueue(engine)
            lease = await claims.claim(job_id, lease_seconds=30)
            assert isinstance(lease, Lease)

            finished = await claims.fail(lease)
            async with engine.connect() as connection:
                status = (
                    await connection.execute(
                        text("SELECT status FROM jobs WHERE id = CAST(:id AS uuid)"),
                        {"id": str(job_id)},
                    )
                ).scalar_one()
            reclaimable = await claims.claim(job_id, lease_seconds=30)
            return finished, status, reclaimable is AlreadyClaimed
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (True, "failed", True)


def test_state_survives_reconnect_after_dependency_restart(db: Settings) -> None:
    """A fresh engine (simulated process restart) sees committed state."""

    async def scenario() -> int:
        engine = create_async_engine(db.database_url)
        job_id = await _enqueue(engine)
        await engine.dispose()

        fresh = create_async_engine(db.database_url)
        try:
            async with fresh.connect() as connection:
                return (
                    await connection.execute(
                        text("SELECT count(*) FROM jobs WHERE id = CAST(:id AS uuid)"),
                        {"id": str(job_id)},
                    )
                ).scalar_one()
        finally:
            await fresh.dispose()

    assert asyncio.run(scenario()) == 1


# --- object store -------------------------------------------------------------


def test_object_store_is_manifest_addressed_and_checksum_verifying(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "storage")

    address = store.put(b"hello knowledge base")
    again = store.put(b"hello knowledge base")

    assert address == again  # content-addressed: identical bytes, identical manifest
    assert store.get(address) == b"hello knowledge base"
    manifest = store.manifest(address)
    assert manifest["size"] == len(b"hello knowledge base")

    (tmp_path / "storage" / "objects" / address).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        store.get(address)

    store.delete(address)
    with pytest.raises(FileNotFoundError):
        store.get(address)


# --- managed backup inventory port --------------------------------------------


def test_backup_inventory_is_deletion_aware() -> None:
    inventory = InMemoryManagedBackupInventory(
        [
            ManagedBackup(
                id="b1",
                path="backups/nightly.tar.enc",
                created_at=datetime.now(UTC),
                source_ids=frozenset({"s1", "s2"}),
            ),
            ManagedBackup(
                id="b2",
                path="backups/weekly.tar.enc",
                created_at=datetime.now(UTC),
                source_ids=frozenset({"s3"}),
            ),
        ]
    )

    assert [backup.id for backup in inventory.list_containing("s1")] == ["b1"]

    purged = inventory.purge_containing("s2")

    assert purged == 1
    assert inventory.list_containing("s1") == []
    assert [backup.id for backup in inventory.list_containing("s3")] == ["b2"]
    assert inventory.purge_containing("s2") == 0  # idempotent
