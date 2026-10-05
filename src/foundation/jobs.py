"""Job queue, outbox dispatcher, and leases (Task 5).

PostgreSQL is authoritative: ``JobQueue.enqueue`` writes the job record and
its outbox entry in the caller's transaction, and ``JobDispatcher`` relays
committed entries to Dramatiq (Redis), marking publication separately from
execution. Delivery is at-least-once; consumers deduplicate through
``JobClaims``, whose leases are the only path to a completed job.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from dramatiq import Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@dataclass(frozen=True)
class JobCommand:
    type: str
    payload: dict
    idempotency_key: str | None = None


@dataclass(frozen=True)
class Lease:
    job_id: uuid.UUID
    token: str
    expires_at: datetime


class _AlreadyClaimed:
    """Sentinel singleton: another consumer holds the lease or the job is done."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AlreadyClaimed"


AlreadyClaimed = _AlreadyClaimed()


class JobQueue:
    async def enqueue(self, command: JobCommand, transaction: AsyncConnection) -> uuid.UUID:
        if command.idempotency_key is not None:
            existing = (
                await transaction.execute(
                    text("SELECT id FROM jobs WHERE idempotency_key = :key"),
                    {"key": command.idempotency_key},
                )
            ).scalar_one_or_none()
            if existing is not None:
                # asyncpg may hand back a native uuid.UUID or a string.
                return existing if isinstance(existing, uuid.UUID) else uuid.UUID(str(existing))

        job_id = uuid.uuid4()
        await transaction.execute(
            text(
                "INSERT INTO jobs (id, type, payload, status, idempotency_key, created_at, updated_at) "
                "VALUES (:id, :type, CAST(:payload AS jsonb), 'pending', :key, now(), now())"
            ),
            {
                "id": str(job_id),
                "type": command.type,
                "payload": _json(command.payload),
                "key": command.idempotency_key,
            },
        )
        # The outbox entry, not Redis, is what makes the job dispatchable.
        await transaction.execute(
            text(
                "INSERT INTO outbox (id, topic, payload, created_at) "
                "VALUES (:id, :topic, CAST(:payload AS jsonb), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "topic": command.type,
                "payload": _json({"job_id": str(job_id), **command.payload}),
            },
        )
        return job_id


class JobDispatcher:
    """Relay committed outbox entries to the broker; mark after sending.

    Sending before marking is deliberate: a crash in between re-sends the
    entry (at-least-once). Marking before sending could lose a job forever.
    """

    def __init__(self, engine: AsyncEngine, broker) -> None:
        self._engine = engine
        self._broker = broker

    async def relay(self, batch: int = 100) -> int:
        async with self._engine.begin() as connection:
            entries = (
                await connection.execute(
                    text(
                        "SELECT id, topic, payload FROM outbox "
                        "WHERE published_at IS NULL "
                        "ORDER BY created_at LIMIT :batch FOR UPDATE SKIP LOCKED"
                    ),
                    {"batch": batch},
                )
            ).all()

        sent = 0
        for entry in entries:
            self._broker.enqueue(
                Message(
                    queue_name="default",
                    actor_name=entry.topic,
                    args=(),
                    kwargs=entry.payload,  # asyncpg decodes jsonb to Python objects
                    options={},
                )
            )
            async with self._engine.begin() as connection:
                await connection.execute(
                    text("UPDATE outbox SET published_at = now() WHERE id = :id"),
                    {"id": str(entry.id)},
                )
            sent += 1
        return sent


class JobClaims:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim(self, job_id: uuid.UUID, lease_seconds: float = 60) -> Lease | _AlreadyClaimed:
        token = uuid.uuid4().hex
        async with self._engine.begin() as connection:
            claimed = (
                await connection.execute(
                    text(
                        "UPDATE jobs SET status = 'running', lease_owner = :token, "
                        "lease_expires_at = now() + make_interval(secs => :lease), "
                        "attempts = attempts + 1, updated_at = now() "
                        "WHERE id = :id AND "
                        "(status = 'pending' OR (status = 'running' AND lease_expires_at < now())) "
                        "RETURNING lease_expires_at"
                    ),
                    {"token": token, "lease": lease_seconds, "id": str(job_id)},
                )
            ).first()
        if claimed is None:
            return AlreadyClaimed
        return Lease(job_id=job_id, token=token, expires_at=claimed.lease_expires_at)

    async def renew(self, lease: Lease, lease_seconds: float = 60) -> bool:
        async with self._engine.begin() as connection:
            result = (
                await connection.execute(
                    text(
                        "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => :lease), "
                        "updated_at = now() "
                        "WHERE id = :id AND lease_owner = :token AND status = 'running' "
                        "RETURNING lease_expires_at"
                    ),
                    {"lease": lease_seconds, "id": str(lease.job_id), "token": lease.token},
                )
            ).first()
        return result is not None

    async def complete(self, lease: Lease) -> bool:
        return await self._finish(lease, "completed")

    async def fail(self, lease: Lease) -> bool:
        return await self._finish(lease, "failed")

    async def _finish(self, lease: Lease, status: str) -> bool:
        async with self._engine.begin() as connection:
            result = (
                await connection.execute(
                    text(
                        "UPDATE jobs SET status = :status, lease_owner = NULL, "
                        "lease_expires_at = NULL, updated_at = now() "
                        "WHERE id = :id AND lease_owner = :token AND status = 'running' "
                        "RETURNING id"
                    ),
                    {"status": status, "id": str(lease.job_id), "token": lease.token},
                )
            ).first()
        return result is not None


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)
