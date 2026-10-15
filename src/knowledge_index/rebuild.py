"""Concurrent-safe embedding index rebuild (Task 18).

Switching embedding compatibility requires re-indexing every member version
under a new key, which takes long enough that the knowledge base changes
underway: replacements cut over and sources are deleted mid-rebuild. The
design (spec §94: "replays concurrent source changes from a durable ledger
and verifies current membership before cutover"):

- ``start_rebuild`` snapshots the outbox watermark and enqueues one worker
  job from the committed outbox row (Task 5 discipline).
- Membership is *derived*, never snapshotted: at any moment a member is the
  live ``active`` version or the ``indexed`` replacement of a non-deleted
  source. Mid-rebuild lifecycle changes therefore self-heal; the outbox
  ledger is consumed to know when the world has stopped moving and to drop
  candidates of deleted sources promptly.
- ``stage_candidate`` writes one idempotent staging generation per member —
  a crash loses only the job redelivery, not the embedding work.
- ``activate_candidate`` is the short final lock: one transaction takes the
  workspace advisory lock (replacement cutovers and deletion requests take
  the same lock), drains the ledger *inside the cutover transaction*, verifies
  exact membership coverage, then retires every non-target generation and
  activates the candidates atomically. Anything less than full coverage is a
  safe refusal — nothing is retired, the old index keeps serving.

Because cutover also activates the target-key candidate of ``indexed``
replacements (which are not yet retrievable), an operator cutover after the
rebuild always finds a covered live version: parity, never a bare source.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from ai_providers.contracts import EmbeddingDimensionError, EmbeddingProvider
from foundation.events import AuditEvent, EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from ingestion.chunking import ChunkPolicy, chunk
from ingestion.extraction import SourceObject, extract
from knowledge_index.generations import compatibility_key
from knowledge_index.indexer import IndexingError, insert_chunk

__all__ = [
    "ACTIVATED_EVENT",
    "DELETED_EVENT",
    "Rebuild",
    "RebuildError",
    "RebuildNotConverged",
    "StagedCandidate",
    "activate_candidate",
    "apply_outbox_event",
    "drain_outbox",
    "rebuild_status",
    "stage_candidate",
    "start_rebuild",
    "verify_candidate",
]

ACTIVATED_EVENT = "source_version_activated"
DELETED_EVENT = "source_deleted"
LIFECYCLE_TOPICS = (ACTIVATED_EVENT, DELETED_EVENT)

# The short final lock: cutover, replacement activation (Task 16), and the
# deletion request (Task 17) serialize on this per-workspace lock, so the
# live set cannot change between the final drain and the cutover commit.
ADVISORY_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:ws AS text), 0))"

# A member is covered when a complete target-key generation exists for it.
_COVERED = """
EXISTS (
  SELECT 1 FROM index_generations g
  WHERE g.source_version_id = v.id
    AND g.compatibility_key = :key AND g.dimension = :dim
    AND g.state IN ('staging', 'active') AND g.chunk_count > 0
    AND NOT EXISTS (
      SELECT 1 FROM index_chunks c
      WHERE c.generation_id = g.id AND vector_dims(c.embedding) <> :dim
    )
)
"""

REBUILD_MESSAGES = {
    "already_running": "an index rebuild is already running for this workspace",
    "not_found": "index rebuild does not exist in this workspace",
}


class RebuildError(Exception):
    """The rebuild request is refused; ``reason`` is a stable machine code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(REBUILD_MESSAGES.get(reason, reason))


class RebuildNotConverged(Exception):
    """Membership coverage is incomplete; cutover refused, nothing changed."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"{len(missing)} member version(s) lack a candidate")


@dataclass(frozen=True)
class Rebuild:
    id: str
    workspace_id: str
    target_key: str
    dimension: int
    model: str
    state: str
    watermark: int
    detail: str | None = None


@dataclass(frozen=True)
class StagedCandidate:
    source_version_id: str
    generation_id: str
    chunk_count: int
    reused: bool


async def start_rebuild(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    workspace_id: str,
    profile,
    policy: ChunkPolicy | None = None,
) -> Rebuild:
    """Open the workspace's rebuild under the profile's compatibility key."""
    key = compatibility_key(profile.embedding_model, profile.embedding_dimensions, policy or ChunkPolicy())
    async with uow.transaction() as transaction:
        running = (
            await transaction.execute(
                text("SELECT id FROM index_rebuilds WHERE workspace_id = :ws AND state = 'running'"),
                {"ws": workspace_id},
            )
        ).first()
        if running is not None:
            raise RebuildError("already_running")
        watermark = (
            await transaction.execute(text("SELECT coalesce(max(seq), 0) FROM outbox"))
        ).scalar_one()
        rebuild_id = str(uuid.uuid4())
        await transaction.execute(
            text(
                "INSERT INTO index_rebuilds (id, workspace_id, target_key, dimension, "
                "model, state, watermark) "
                "VALUES (:id, :ws, :key, :dim, :model, 'running', :watermark)"
            ),
            {
                "id": rebuild_id,
                "ws": workspace_id,
                "key": key,
                "dim": profile.embedding_dimensions,
                "model": profile.embedding_model,
                "watermark": int(watermark),
            },
        )
        await writer.record(
            AuditEvent(
                type="index.rebuild_started",
                actor="system",
                subject=rebuild_id,
                metadata={"target_key": key, "watermark": int(watermark)},
            ),
            transaction,
        )
        # Dispatch only from this committed outbox row (Task 5 discipline).
        await JobQueue().enqueue(
            JobCommand(
                type="rebuild_index",
                payload={"rebuild_id": rebuild_id, "workspace_id": workspace_id},
                idempotency_key=f"rebuild_index:{rebuild_id}",
            ),
            transaction,
        )
    return Rebuild(
        id=rebuild_id,
        workspace_id=workspace_id,
        target_key=key,
        dimension=profile.embedding_dimensions,
        model=profile.embedding_model,
        state="running",
        watermark=int(watermark),
    )


async def apply_outbox_event(
    uow: UnitOfWork,
    rebuild_id: str,
    *,
    seq: int,
    topic: str,
    payload: dict,
) -> bool:
    """Consume one ledger entry; duplicate or stale delivery is a no-op."""
    async with uow.transaction() as transaction:
        rebuild = await _locked_rebuild(transaction, rebuild_id)
        if rebuild is None:
            raise RebuildError("not_found")
        return await _apply_event(transaction, rebuild, seq, topic, payload) is not None


async def drain_outbox(uow: UnitOfWork, rebuild_id: str, batch: int = 100) -> int:
    """Consume every lifecycle entry after the watermark; returns the count."""
    consumed = 0
    watermark: int | None = None  # None: not loaded yet
    while True:
        async with uow.transaction() as transaction:
            rebuild = await _locked_rebuild(transaction, rebuild_id)
            if rebuild is None:
                raise RebuildError("not_found")
            if watermark is None:
                watermark = rebuild.watermark
            entries = (
                await transaction.execute(
                    text(
                        "SELECT seq, topic, payload FROM outbox "
                        "WHERE seq > :watermark AND topic = ANY(:topics) "
                        "ORDER BY seq LIMIT :batch"
                    ),
                    {
                        "watermark": watermark,
                        "topics": list(LIFECYCLE_TOPICS),
                        "batch": batch,
                    },
                )
            ).mappings().all()
            if not entries:
                return consumed
            for entry in entries:
                # A stale entry (None) keeps the current watermark.
                watermark = await _apply_event(transaction, rebuild, int(entry["seq"]),
                                               entry["topic"], dict(entry["payload"])) \
                    or watermark
                consumed += 1


async def stage_candidate(
    uow: UnitOfWork,
    embeddings: EmbeddingProvider,
    store: ObjectStore,
    *,
    workspace_id: str,
    version_id: str,
    profile,
    policy: ChunkPolicy | None = None,
) -> StagedCandidate | None:
    """Stage one member's target-key generation; ``None`` if it left the membership.

    Idempotent: an existing complete candidate (even an active one) is reused
    without re-embedding, so crash redelivery converges.
    """
    policy = policy or ChunkPolicy()
    async with uow.transaction() as transaction:
        row = (
            await transaction.execute(
                text(
                    "SELECT v.workspace_id, v.source_id, v.object_sha256, v.media_type, "
                    "v.filename, s.state AS source_state "
                    "FROM source_versions v JOIN sources s ON s.id = v.source_id "
                    "WHERE v.id = :id AND v.workspace_id = :ws"
                ),
                {"id": version_id, "ws": workspace_id},
            )
        ).mappings().first()
        if row is None or row["source_state"] == "deleted":
            return None  # not a member anymore; the loop re-derives membership
        reused = await _existing_candidate(
            transaction, version_id,
            key=compatibility_key(profile.embedding_model, profile.embedding_dimensions, policy),
            dim=profile.embedding_dimensions,
        )
        if reused is not None:
            return StagedCandidate(
                source_version_id=version_id, generation_id=str(reused["id"]),
                chunk_count=int(reused["chunk_count"]), reused=True,
            )

    document = extract(SourceObject(
        source_version_id=version_id,
        media_type=row["media_type"],
        filename=row["filename"],
        data=store.get(str(row["object_sha256"])),
    ))
    chunks = chunk(document, policy)
    if not chunks:
        raise IndexingError("empty_content")
    result = embeddings.embed([item.text for item in chunks])
    if result.dimensions != profile.embedding_dimensions:
        raise EmbeddingDimensionError(
            f"provider returned {result.dimensions} dimensions, "
            f"profile expects {profile.embedding_dimensions}"
        )

    generation_id = str(uuid.uuid4())
    key = compatibility_key(profile.embedding_model, profile.embedding_dimensions, policy)
    async with uow.transaction() as transaction:
        await transaction.execute(
            text(
                "INSERT INTO index_generations (id, workspace_id, source_version_id, "
                "compatibility_key, dimension, model, state, chunk_count) "
                "VALUES (:id, :ws, :version, :key, :dim, :model, 'staging', :count)"
            ),
            {
                "id": generation_id,
                "ws": workspace_id,
                "version": version_id,
                "key": key,
                "dim": profile.embedding_dimensions,
                "model": profile.embedding_model,
                "count": len(chunks),
            },
        )
        for ordinal, (chunk_row, vector) in enumerate(zip(chunks, result.vectors)):
            await insert_chunk(transaction, generation_id, workspace_id,
                               version_id, ordinal, chunk_row, vector)
    return StagedCandidate(
        source_version_id=version_id, generation_id=generation_id,
        chunk_count=len(chunks), reused=False,
    )


async def verify_candidate(uow: UnitOfWork, *, workspace_id: str) -> dict:
    """Refuse unless every current member has a complete candidate."""
    async with uow.transaction() as transaction:
        return await _verify(transaction, workspace_id=workspace_id)


async def activate_candidate(
    uow: UnitOfWork,
    writer: EventWriter,
    *,
    workspace_id: str,
    rebuild_id: str,
) -> dict:
    """The short final lock: drain, verify, retire, activate — one transaction.

    Returns ``{"activated": n, "retired": m}``; an already-finished rebuild is
    an idempotent no-op. Incomplete coverage raises ``RebuildNotConverged``
    with the workspace state untouched.
    """
    async with uow.transaction() as transaction:
        await transaction.execute(text(ADVISORY_LOCK), {"ws": workspace_id})
        rebuild = await _locked_rebuild(transaction, rebuild_id)
        if rebuild is None:
            raise RebuildError("not_found")
        if rebuild.state != "running":
            return {"activated": 0, "retired": 0}

        # Same-transaction event consumption: the membership check below sees
        # every lifecycle change committed up to this transaction.
        watermark = rebuild.watermark
        while True:
            entries = (
                await transaction.execute(
                    text(
                        "SELECT seq, topic, payload FROM outbox "
                        "WHERE seq > :watermark AND topic = ANY(:topics) ORDER BY seq LIMIT 100"
                    ),
                    {"watermark": watermark, "topics": list(LIFECYCLE_TOPICS)},
                )
            ).mappings().all()
            if not entries:
                break
            for entry in entries:
                # A stale entry (None) keeps the current watermark.
                watermark = await _apply_event(
                    transaction, rebuild, int(entry["seq"]),
                    entry["topic"], dict(entry["payload"]),
                ) or watermark

        await _verify(transaction, workspace_id=workspace_id)  # raises unless covered

        retired = (
            await transaction.execute(
                text(
                    "UPDATE index_generations SET state = 'retired' "
                    "WHERE workspace_id = :ws AND state IN ('active', 'staging') "
                    "AND compatibility_key <> :key"
                ),
                {"ws": workspace_id, "key": rebuild.target_key},
            )
        ).rowcount or 0
        activated = (
            await transaction.execute(
                text(
                    "UPDATE index_generations g SET state = 'active', activated_at = now() "
                    "WHERE g.workspace_id = :ws AND g.compatibility_key = :key "
                    "AND g.state = 'staging' AND EXISTS ("
                    "  SELECT 1 FROM source_versions v JOIN sources s ON s.id = v.source_id"
                    "  WHERE v.id = g.source_version_id AND v.workspace_id = :ws"
                    "    AND v.state IN ('active', 'indexed') AND s.state <> 'deleted')"
                ),
                {"ws": workspace_id, "key": rebuild.target_key},
            )
        ).rowcount or 0
        await transaction.execute(
            text(
                "UPDATE index_rebuilds SET state = 'completed', updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": rebuild_id},
        )
        await writer.record(
            AuditEvent(
                type="index.rebuild_activated",
                actor="system",
                subject=rebuild_id,
                metadata={
                    "target_key": rebuild.target_key,
                    "activated": int(activated),
                    "retired": int(retired),
                },
            ),
            transaction,
        )
    return {"activated": int(activated), "retired": int(retired)}


async def rebuild_status(uow: UnitOfWork, *, workspace_id: str) -> dict | None:
    """Latest rebuild (running first) with current membership coverage."""
    async with uow.transaction() as transaction:
        row = (
            await transaction.execute(
                text(
                    "SELECT id, target_key, dimension, model, state, watermark, detail "
                    "FROM index_rebuilds WHERE workspace_id = :ws "
                    "ORDER BY (state = 'running') DESC, created_at DESC LIMIT 1"
                ),
                {"ws": workspace_id},
            )
        ).mappings().first()
        if row is None:
            return None
        coverage = await _coverage(transaction, workspace_id=workspace_id, row=row)
    return {
        "id": str(row["id"]),
        "target_key": row["target_key"],
        "state": row["state"],
        "watermark": int(row["watermark"]),
        "detail": row["detail"],
        "live": coverage["live"],
        "covered": coverage["covered"],
        "missing": coverage["missing"],
    }


# --- internals (caller holds the transaction) -------------------------------


async def _locked_rebuild(transaction, rebuild_id: str) -> Rebuild | None:
    row = (
        await transaction.execute(
            text("SELECT * FROM index_rebuilds WHERE id = :id FOR UPDATE"),
            {"id": rebuild_id},
        )
    ).mappings().first()
    if row is None:
        return None
    return Rebuild(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        target_key=row["target_key"],
        dimension=int(row["dimension"]),
        model=row["model"],
        state=row["state"],
        watermark=int(row["watermark"]),
        detail=row["detail"],
    )


async def _apply_event(transaction, rebuild: Rebuild, seq: int, topic: str, payload: dict) -> int | None:
    """Apply one ledger entry to a locked rebuild; stale replays return None,
    fresh ones the new watermark."""
    if seq <= rebuild.watermark:
        return None
    if topic == DELETED_EVENT:
        await _drop_staging(transaction, str(rebuild.workspace_id), str(payload["source_id"]),
                            key=rebuild.target_key)
    await transaction.execute(
        text("UPDATE index_rebuilds SET watermark = :seq, updated_at = now() WHERE id = :id"),
        {"seq": seq, "id": rebuild.id},
    )
    return seq


async def _drop_staging(transaction, workspace_id: str, source_id: str, *, key: str) -> None:
    """Discard the rebuild's staged candidates for a deleted source's versions."""
    await transaction.execute(
        text(
            "DELETE FROM index_chunks WHERE generation_id IN ("
            "  SELECT g.id FROM index_generations g"
            "  JOIN source_versions v ON v.id = g.source_version_id"
            "  WHERE v.source_id = :source AND v.workspace_id = :ws"
            "    AND g.compatibility_key = :key AND g.state = 'staging')"
        ),
        {"source": source_id, "ws": workspace_id, "key": key},
    )
    await transaction.execute(
        text(
            "DELETE FROM index_generations g USING source_versions v "
            "WHERE v.id = g.source_version_id AND v.source_id = :source "
            "AND v.workspace_id = :ws AND g.compatibility_key = :key "
            "AND g.state = 'staging'"
        ),
        {"source": source_id, "ws": workspace_id, "key": key},
    )


async def _verify(transaction, *, workspace_id: str) -> dict:
    coverage = await _coverage(transaction, workspace_id=workspace_id, row=None)
    if coverage["missing"]:
        raise RebuildNotConverged(coverage["missing"])
    return {"live": coverage["live"], "covered": coverage["covered"]}


async def _coverage(transaction, *, workspace_id: str, row) -> dict:
    """Membership coverage; ``row`` supplies the target key when given."""
    params: dict = {"ws": workspace_id}
    if row is not None:
        params.update({"key": row["target_key"], "dim": int(row["dimension"])})
    else:
        latest = (
            await transaction.execute(
                text(
                    "SELECT target_key, dimension FROM index_rebuilds "
                    "WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT 1"
                ),
                {"ws": workspace_id},
            )
        ).mappings().first()
        if latest is None:
            return {"live": 0, "covered": 0, "missing": []}
        params.update({"key": latest["target_key"], "dim": int(latest["dimension"])})
    rows = (
        await transaction.execute(
            text(
                "SELECT v.id AS version_id, " + _COVERED + " AS covered "
                "FROM source_versions v JOIN sources s ON s.id = v.source_id "
                "WHERE v.workspace_id = :ws AND s.workspace_id = :ws "
                "AND s.state <> 'deleted' AND v.state IN ('active', 'indexed')"
            ),
            params,
        )
    ).mappings().all()
    missing = [str(r["version_id"]) for r in rows if not r["covered"]]
    return {
        "live": len(rows),
        "covered": len(rows) - len(missing),
        "missing": missing,
    }


async def _existing_candidate(transaction, version_id: str, *, key: str, dim: int):
    return (
        await transaction.execute(
            text(
                "SELECT g.id, g.chunk_count FROM index_generations g "
                "WHERE g.source_version_id = :version AND g.compatibility_key = :key "
                "AND g.dimension = :dim AND g.state IN ('staging', 'active') "
                "AND g.chunk_count > 0 "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM index_chunks c WHERE c.generation_id = g.id "
                "  AND vector_dims(c.embedding) <> :dim) "
                "ORDER BY g.created_at, g.id LIMIT 1"
            ),
            {"version": version_id, "key": key, "dim": dim},
        )
    ).mappings().first()
