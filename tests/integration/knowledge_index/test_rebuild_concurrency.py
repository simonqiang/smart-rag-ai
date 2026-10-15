"""Task 18: concurrent-safe embedding index rebuild.

The rebuild stages a target-compatibility generation for every member
version (live ``active`` versions and ``indexed`` replacements awaiting
cutover), consumes lifecycle events from the transactional outbox, and cuts
over only after the final drain inside the cutover transaction proves the
candidate set exactly covers current membership. Every scenario interleaves
real lifecycle calls (replacement activation, owner deletion, duplicate
event replay) at deterministic points; the seed matrix varies the injection
point and asserts the same invariants after cutover.
"""

import asyncio
import random
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_providers.fakes import FakeEmbeddingProvider
from foundation.config import ModelProfile
from foundation.events import EventWriter
from foundation.jobs import JobCommand, JobQueue
from foundation.storage import ObjectStore
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from ingestion.extraction import SourceObject, extract
from ingestion.uploads import register_upload
from knowledge_index.generations import ActivationError, activate_generation
from knowledge_index.indexer import IndexingError, index_version
from knowledge_index.rebuild import (
    RebuildError,
    RebuildNotConverged,
    activate_candidate,
    apply_outbox_event,
    drain_outbox,
    rebuild_status,
    stage_candidate,
    start_rebuild,
    verify_candidate,
)
from source_catalog.deletion import request_permanent_deletion
from source_catalog.version_service import activate_ready_version

OLD_KEY = "fake-embed:8:chunk-v1:lang-v1"
NEW_KEY = "fake-embed-v2:8:chunk-v1:lang-v1"
OLD_PROFILE = ModelProfile(
    name="old", chat_model="fake-chat", embedding_model="fake-embed", embedding_dimensions=8,
)
NEW_PROFILE = ModelProfile(
    name="new", chat_model="fake-chat", embedding_model="fake-embed-v2", embedding_dimensions=8,
)
TINY_PROFILE = ModelProfile(
    name="tiny", chat_model="fake-chat", embedding_model="fake-embed-v2", embedding_dimensions=4,
)
CONTENT = b"Smart RAG keeps answers grounded in cited evidence.\nEvery claim cites a passage."
ACTIVATED = "source_version_activated"
DELETED = "source_deleted"


def _run(db, coro_factory, store_root: Path | None = None) -> object:
    async def run() -> object:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        store = ObjectStore(store_root) if store_root else None
        try:
            return await coro_factory(engine, uow, store)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _owner(workspace: dict) -> AccessContext:
    return AccessContext(
        user_id=workspace["owner_id"], workspace_id=workspace["workspace_id"], role="owner"
    )


def _upload(db, workspace: dict, store_root: Path, *, source_id: str | None = None,
            data: bytes = CONTENT) -> str:
    """Real upload path; returns the new version id in state ``uploaded``."""

    async def run(_engine, uow, store) -> str:
        accepted = await register_upload(
            uow, EventWriter(), store,
            context=_owner(workspace),
            collection_id=workspace["collection_id"],
            name="Handbook",
            filename="handbook.txt",
            data=data,
            source_id=source_id,
        )
        return accepted.source_version_id

    return _run(db, run, store_root)


def _extract(db, store_root: Path, version_id: str) -> None:
    async def run(engine, uow, store) -> None:
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT media_type, filename, object_sha256 FROM source_versions "
                         "WHERE id = :id"),
                    {"id": version_id},
                )
            ).mappings().one()
            extract(SourceObject(
                source_version_id=version_id,
                media_type=row["media_type"],
                filename=row["filename"],
                data=store.get(row["object_sha256"]),
            ))
            await transaction.execute(
                text("UPDATE source_versions SET state = 'extracted' WHERE id = :id"),
                {"id": version_id},
            )

    _run(db, run, store_root)


def _index_and_activate(db, workspace: dict, store_root: Path, version_id: str) -> str:
    """Stage under the old profile and activate; returns the generation id."""

    async def run(_engine, uow, store) -> str:
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT workspace_id, media_type, filename, object_sha256 "
                         "FROM source_versions WHERE id = :id"),
                    {"id": version_id},
                )
            ).mappings().one()
        document = extract(SourceObject(
            source_version_id=version_id,
            media_type=row["media_type"],
            filename=row["filename"],
            data=store.get(row["object_sha256"]),
        ))
        staged = await index_version(
            uow, FakeEmbeddingProvider(OLD_PROFILE.embedding_dimensions),
            workspace_id=str(row["workspace_id"]),
            source_version_id=version_id,
            document=document,
            profile=OLD_PROFILE,
        )
        await activate_generation(
            uow, EventWriter(), staged.generation_id,
            workspace_id=workspace["workspace_id"],
        )
        return staged.generation_id

    return _run(db, run, store_root)


@pytest.fixture()
def world(db, workspace, store_root):
    """Two live sources under the old key plus an ``indexed`` replacement.

    alpha and beta are indexed and active under OLD_PROFILE. beta also has a
    replacement version indexed under the old key awaiting cutover — the
    parity case: it must keep generation coverage through the rebuild.
    """
    alpha_version = _upload(db, workspace, store_root)
    _extract(db, store_root, alpha_version)
    _index_and_activate(db, workspace, store_root, alpha_version)

    beta_version = _upload(db, workspace, store_root)
    _extract(db, store_root, beta_version)
    _index_and_activate(db, workspace, store_root, beta_version)

    beta_source = str(_run(db, lambda engine, uow, store: _scalar(
        engine, "SELECT source_id FROM source_versions WHERE id = :id",
        {"id": beta_version},
    )))  # asyncpg returns a native UUID; keep it a string for payloads
    replacement = _upload(db, workspace, store_root, source_id=beta_source)
    _extract(db, store_root, replacement)
    _index_and_activate(db, workspace, store_root, replacement)

    return {
        "alpha_version": alpha_version,
        "beta_version": beta_version,
        "beta_source": beta_source,
        "replacement": replacement,
    }


async def _scalar(engine, query: str, params: dict):
    async with engine.begin() as connection:
        return (
            await connection.execute(text(query), params)
        ).scalar_one()


def _gens(db, store_root: Path | None = None) -> list[dict]:
    async def run(engine, _uow, _store) -> list[dict]:
        async with engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT source_version_id AS version, compatibility_key AS key, "
                        "state FROM index_generations ORDER BY created_at, id"
                    )
                )
            ).mappings().all()
        return [
            {"version": str(row["version"]), "key": row["key"], "state": row["state"]}
            for row in rows
        ]

    return _run(db, run, store_root)


def _start(db, workspace: dict, profile: ModelProfile = NEW_PROFILE) -> object:
    async def run(_engine, uow, _store) -> object:
        return await start_rebuild(
            uow, EventWriter(), workspace_id=workspace["workspace_id"], profile=profile,
        )

    return _run(db, run)


def _stage(db, workspace: dict, store_root: Path, version_id: str,
           embeddings: FakeEmbeddingProvider | None = None,
           profile: ModelProfile = NEW_PROFILE) -> object:
    async def run(_engine, uow, store) -> object:
        return await stage_candidate(
            uow, embeddings or FakeEmbeddingProvider(profile.embedding_dimensions), store,
            workspace_id=workspace["workspace_id"], version_id=version_id, profile=profile,
        )

    return _run(db, run, store_root)


async def _verify(uow, workspace: dict) -> dict:
    return await verify_candidate(uow, workspace_id=workspace["workspace_id"])


async def _cut(uow, workspace: dict, rebuild_id: str) -> dict:
    return await activate_candidate(
        uow, EventWriter(), workspace_id=workspace["workspace_id"], rebuild_id=rebuild_id,
    )


def _activate_replacement(db, workspace: dict, world: dict, store_root: Path) -> None:
    async def run(_engine, uow, _store) -> None:
        await activate_ready_version(
            uow, EventWriter(),
            context=_owner(workspace),
            source_id=world["beta_source"],
            version_id=world["replacement"],
        )

    _run(db, run, store_root)


def _delete_source(db, workspace: dict, store_root: Path, source_id: str) -> None:
    async def run(_engine, uow, _store) -> None:
        await request_permanent_deletion(
            uow, EventWriter(), context=_owner(workspace), source_id=source_id,
        )

    _run(db, run, store_root)


def _activated_payload(workspace: dict, world: dict) -> dict:
    return {
        "workspace_id": workspace["workspace_id"],
        "source_id": world["beta_source"],
        "version_id": world["replacement"],
    }


def _enqueue_rebuild(db, workspace: dict, rebuild) -> str:
    """The real dispatch path: a committed outbox row carries the job id."""

    async def run() -> str:
        engine = create_async_engine(db.database_url)
        uow = UnitOfWork(engine)
        try:
            async with uow.transaction() as transaction:
                job_id = await JobQueue().enqueue(
                    JobCommand(
                        type="rebuild_index",
                        payload={"rebuild_id": rebuild.id,
                                 "workspace_id": workspace["workspace_id"]},
                        idempotency_key=f"rebuild_index:{rebuild.id}",
                    ),
                    transaction,
                )
            return str(job_id)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_start_rebuild_records_target_and_enqueues_job(db, workspace, world, store_root) -> None:
    rebuild = _start(db, workspace)

    assert rebuild.state == "running"
    assert rebuild.target_key == NEW_KEY
    assert rebuild.dimension == 8

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(text("SELECT * FROM index_rebuilds"))
            ).mappings().one()
            watermark = (
                # The rebuild's own job entry is not a lifecycle event.
                await transaction.execute(
                    text("SELECT coalesce(max(seq), 0) FROM outbox "
                         "WHERE topic <> 'rebuild_index'")
                )
            ).scalar_one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type = 'index.rebuild_started'")
                )
            ).scalars().all()
            jobs = (
                await transaction.execute(
                    text("SELECT topic FROM outbox WHERE topic = 'rebuild_index'")
                )
            ).scalars().all()
        return dict(row), int(watermark), list(audits), list(jobs)

    stored, watermark, audits, jobs = _run(db, verify, store_root)
    assert stored["target_key"] == rebuild.target_key
    assert stored["watermark"] == watermark  # snapshot of the ledger at start
    assert audits == ["index.rebuild_started"]
    assert jobs == ["rebuild_index"]

    with pytest.raises(RebuildError) as excinfo:
        _start(db, workspace)
    assert excinfo.value.reason == "already_running"


def test_stage_candidate_stages_without_touching_the_live_index(
    db, workspace, world, store_root,
) -> None:
    staged = _stage(db, workspace, store_root, world["alpha_version"])

    assert staged.reused is False
    assert staged.chunk_count == 1
    assert staged.generation_id

    # The active old-key generation still serves retrieval unchanged.
    assert {"version": world["alpha_version"], "key": OLD_KEY, "state": "active"} \
        in _gens(db, store_root)
    assert {"version": world["alpha_version"], "key": NEW_KEY, "state": "staging"} \
        in _gens(db, store_root)

    again = _stage(db, workspace, store_root, world["alpha_version"])
    assert again.reused is True
    assert again.generation_id == staged.generation_id


def test_cutover_activates_exactly_current_membership(db, workspace, world, store_root) -> None:
    rebuild = _start(db, workspace)
    for version in (world["alpha_version"], world["beta_version"], world["replacement"]):
        _stage(db, workspace, store_root, version)
    counts = _run(db, lambda engine, uow, store: _verify(uow, workspace), store_root)
    assert counts == {"live": 3, "covered": 3}

    result = _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)

    assert result["activated"] == 3  # both live versions and the staged replacement
    generations = _gens(db, store_root)
    # Every old-key generation retired; every member covered by an active
    # target-key generation. The replacement stays 'indexed' (not live) but
    # keeps parity coverage for a later operator cutover.
    assert all(row["state"] == "retired" for row in generations if row["key"] != NEW_KEY)
    for version in (world["alpha_version"], world["beta_version"], world["replacement"]):
        assert {"version": version, "key": NEW_KEY, "state": "active"} in generations

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            state = (
                await transaction.execute(
                    text("SELECT state FROM index_rebuilds WHERE id = :id"),
                    {"id": rebuild.id},
                )
            ).scalar_one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type = 'index.rebuild_activated'")
                )
            ).scalars().all()
        return state, list(audits)

    state, audits = _run(db, verify, store_root)
    assert state == "completed"
    assert audits == ["index.rebuild_activated"]

    # Idempotent: a redelivered cutover after completion changes nothing.
    again = _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)
    assert again["activated"] == 0


def test_apply_outbox_event_ignores_duplicate_replay(db, workspace, world, store_root) -> None:
    rebuild = _start(db, workspace)
    _activate_replacement(db, workspace, world, store_root)  # emits the ledger entry

    async def last_event(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT seq, topic, payload FROM outbox WHERE topic = :topic "
                         "ORDER BY seq DESC LIMIT 1"),
                    {"topic": ACTIVATED},
                )
            ).mappings().one()
        return int(row["seq"]), row["topic"], dict(row["payload"])

    seq, topic, payload = _run(db, last_event, store_root)

    assert _run(db, lambda engine, uow, store: apply_outbox_event(
        uow, rebuild.id, seq=seq, topic=topic, payload=payload,
    ), store_root) is True
    # Duplicate delivery of the same ledger entry is a no-op.
    assert _run(db, lambda engine, uow, store: apply_outbox_event(
        uow, rebuild.id, seq=seq, topic=topic, payload=payload,
    ), store_root) is False

    status = _run(db, lambda engine, uow, store: rebuild_status(
        uow, workspace_id=workspace["workspace_id"],
    ), store_root)
    assert status["watermark"] == seq


def test_watermark_catch_up_drops_deleted_candidate(
    db, workspace, world, store_root,
) -> None:
    """Events emitted before the first tick are consumed at catch-up."""
    rebuild = _start(db, workspace)
    for version in (world["alpha_version"], world["beta_version"], world["replacement"]):
        _stage(db, workspace, store_root, version)
    _delete_source(db, workspace, store_root, world["beta_source"])

    consumed = _run(db, lambda engine, uow, store: drain_outbox(
        uow, rebuild.id,
    ), store_root)
    assert consumed >= 1  # the tombstone event

    status = _run(db, lambda engine, uow, store: rebuild_status(
        uow, workspace_id=workspace["workspace_id"],
    ), store_root)
    assert status["live"] == 1  # beta tombstoned: only alpha remains a member
    assert status["covered"] == 1

    generations = _gens(db, store_root)
    # The staging candidate of the deleted source's versions is dropped.
    assert not any(
        row["version"] in (world["beta_version"], world["replacement"])
        for row in generations
        if row["state"] == "staging"
    )


def test_activation_during_rebuild_keeps_replacement_parity(
    db, workspace, world, store_root,
) -> None:
    """A mid-rebuild cutover to the replacement converges on the new live set."""
    rebuild = _start(db, workspace)
    for version in (world["alpha_version"], world["beta_version"], world["replacement"]):
        _stage(db, workspace, store_root, version)
    _activate_replacement(db, workspace, world, store_root)

    result = _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)
    assert result["activated"] >= 2

    generations = _gens(db, store_root)
    assert {"version": world["replacement"], "key": NEW_KEY, "state": "active"} in generations
    # The superseded version keeps no active generation; the workspace holds
    # no old-key vector that could mix with new-profile queries.
    assert not any(
        row["version"] == world["beta_version"] and row["state"] == "active"
        for row in generations
    )

    async def version_state(_engine, uow, _store) -> str:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(
                    text("SELECT state FROM source_versions WHERE id = :id"),
                    {"id": world["beta_version"]},
                )
            ).scalar_one()

    assert _run(db, version_state, store_root) == "superseded"


def test_deletion_during_rebuild_never_resurrects_the_source(
    db, workspace, world, store_root,
) -> None:
    rebuild = _start(db, workspace)
    for version in (world["alpha_version"], world["beta_version"], world["replacement"]):
        _stage(db, workspace, store_root, version)
    _delete_source(db, workspace, store_root, world["beta_source"])

    result = _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)
    assert result["activated"] == 1  # alpha only

    assert not any(
        row["version"] in (world["beta_version"], world["replacement"])
        and row["state"] == "active"
        for row in _gens(db, store_root)
    )


def test_crash_before_acknowledgement_resumes_without_reembedding(
    db, workspace, world, store_root,
) -> None:
    """A crash after staging member A loses only the job, not the work."""
    rebuild = _start(db, workspace)
    embeddings = FakeEmbeddingProvider(NEW_PROFILE.embedding_dimensions)
    _stage(db, workspace, store_root, world["alpha_version"], embeddings)
    staged_calls = len(embeddings.requests)

    # Redelivery (crash before acknowledgement): member A reuses its staged
    # generation, only the remaining members embed.
    _stage(db, workspace, store_root, world["beta_version"], embeddings)
    _stage(db, workspace, store_root, world["replacement"], embeddings)
    assert len(embeddings.requests) == staged_calls + 2

    result = _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)
    assert result["activated"] == 3


def test_crash_after_cutover_redelivery_is_idempotent(
    db, workspace, world, store_root,
) -> None:
    from apps.worker.tasks.rebuild_index import rebuild_payload

    rebuild = _start(db, workspace)
    for version in (world["alpha_version"], world["beta_version"], world["replacement"]):
        _stage(db, workspace, store_root, version)
    _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)
    generations_before = _gens(db, store_root)

    # The completed mark was lost (crash after cutover commit): redelivery
    # sees convergence and completes without touching anything.
    job_id = _enqueue_rebuild(db, workspace, rebuild)

    async def redeliver(engine, _uow, store) -> str:
        return await rebuild_payload(
            engine, store, NEW_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": job_id, "rebuild_id": rebuild.id,
             "workspace_id": workspace["workspace_id"]},
        )

    assert _run(db, redeliver, store_root) == "completed"
    assert _gens(db, store_root) == generations_before


def test_verify_refuses_and_changes_nothing_when_a_candidate_is_missing(
    db, workspace, world, store_root,
) -> None:
    rebuild = _start(db, workspace)
    _stage(db, workspace, store_root, world["alpha_version"])  # beta never staged

    with pytest.raises(RebuildNotConverged):
        _run(db, lambda engine, uow, store: _verify(uow, workspace), store_root)

    with pytest.raises(RebuildNotConverged):
        _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)

    # Safe refusal: the old index is untouched, the rebuild stays running.
    generations = _gens(db, store_root)
    assert {"version": world["alpha_version"], "key": OLD_KEY, "state": "active"} \
        in generations
    assert not any(row["state"] == "retired" for row in generations)

    async def state(_engine, uow, _store) -> str:
        async with uow.transaction() as transaction:
            return (
                await transaction.execute(
                    text("SELECT state FROM index_rebuilds WHERE id = :id"),
                    {"id": rebuild.id},
                )
            ).scalar_one()

    assert _run(db, state, store_root) == "running"


def test_dimension_mismatch_fails_rebuild_without_cutover(
    db, workspace, world, store_root,
) -> None:
    from apps.worker.tasks.rebuild_index import rebuild_payload

    rebuild = _start(db, workspace, TINY_PROFILE)
    job_id = _enqueue_rebuild(db, workspace, rebuild)

    async def deliver(engine, _uow, store) -> str:
        return await rebuild_payload(
            engine, store, TINY_PROFILE, FakeEmbeddingProvider(8),  # provider stays 8-dim
            {"job_id": job_id, "rebuild_id": rebuild.id,
             "workspace_id": workspace["workspace_id"]},
        )

    assert _run(db, deliver, store_root) == "failed:dimension_mismatch"

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT state, detail FROM index_rebuilds WHERE id = :id"),
                    {"id": rebuild.id},
                )
            ).mappings().one()
            audits = (
                await transaction.execute(
                    text("SELECT type FROM audit_events WHERE type = 'index.rebuild_failed'")
                )
            ).scalars().all()
        return dict(row), list(audits)

    row, audits = _run(db, verify, store_root)
    assert row["state"] == "failed"
    assert row["detail"] == "dimension_mismatch"
    assert audits == ["index.rebuild_failed"]

    # Safe refusal: the old index still serves; nothing was retired.
    assert all(row["state"] != "retired" for row in _gens(db, store_root))

    # The failed rebuild releases the workspace slot.
    restarted = _start(db, workspace)
    assert restarted.id != rebuild.id


def test_activate_generation_blocks_mismatched_key_during_rebuild(
    db, workspace, world, store_root,
) -> None:
    """A stale pre-switch worker must not activate old-key vectors mid-rebuild."""
    _start(db, workspace)

    # A fresh old-key staging generation appears (stale worker redelivery).
    stale_id = str(uuid.uuid4())

    async def seed(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO index_generations (id, workspace_id, source_version_id, "
                    "compatibility_key, dimension, model, state, chunk_count) "
                    "VALUES (:id, :ws, :version, :key, 8, 'fake-embed', 'staging', 1)"
                ),
                {"id": stale_id, "ws": workspace["workspace_id"],
                 "version": world["alpha_version"], "key": OLD_KEY},
            )

    _run(db, seed, store_root)

    async def activate(engine, uow, _store) -> None:
        await activate_generation(
            uow, EventWriter(), stale_id, workspace_id=workspace["workspace_id"],
        )

    with pytest.raises(ActivationError) as excinfo:
        _run(db, activate, store_root)
    assert excinfo.value.reason == "rebuild_in_progress"

    # Target-key activations pass: the normal pipeline keeps working.
    staged = _stage(db, workspace, store_root, world["alpha_version"])

    async def activate_new(engine, uow, _store) -> None:
        await activate_generation(
            uow, EventWriter(), staged.generation_id,
            workspace_id=workspace["workspace_id"],
        )

    _run(db, activate_new, store_root)


def test_worker_payload_converges_end_to_end(db, workspace, world, store_root) -> None:
    from apps.worker.tasks.rebuild_index import rebuild_payload

    rebuild = _start(db, workspace)
    _delete_source(db, workspace, store_root, world["beta_source"])
    job_id = _enqueue_rebuild(db, workspace, rebuild)

    async def deliver(engine, _uow, store) -> str:
        return await rebuild_payload(
            engine, store, NEW_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": job_id, "rebuild_id": rebuild.id,
             "workspace_id": workspace["workspace_id"]},
        )

    assert _run(db, deliver, store_root) == "completed"

    generations = _gens(db, store_root)
    assert {"version": world["alpha_version"], "key": NEW_KEY, "state": "active"} \
        in generations
    assert not any(
        row["version"] in (world["beta_version"], world["replacement"])
        and row["state"] == "active"
        for row in generations
    )
    assert not any(
        row["key"] == OLD_KEY and row["state"] == "active" for row in generations
    )


def _interleave(db, workspace: dict, world: dict, store_root: Path,
                rebuild_id: str, action: str, inject_at: int) -> None:
    """Stage the members, injecting a lifecycle change at one step boundary."""
    members = [world["alpha_version"], world["beta_version"], world["replacement"]]
    for step, version in enumerate(members):
        if step == inject_at:
            if action == "activate":
                _activate_replacement(db, workspace, world, store_root)
            elif action == "delete":
                _delete_source(db, workspace, store_root, world["beta_source"])
            else:  # duplicate replay of an already-consumed ledger entry
                _run(db, lambda engine, uow, store: apply_outbox_event(
                    uow, rebuild_id, seq=0, topic=ACTIVATED,
                    payload=_activated_payload(workspace, world),
                ), store_root)
        _stage(db, workspace, store_root, version)


@pytest.mark.parametrize("seed", range(10))
def test_seed_matrix_interleaved_lifecycle_changes_converge(
    db, workspace, world, store_root, seed,
) -> None:
    """Deterministic seed matrix: a lifecycle change (or duplicate replay)
    injected at a seeded staging step must still converge exactly."""
    rng = random.Random(seed)
    action = rng.choice(["activate", "delete", "duplicate"])
    inject_at = rng.randint(0, 2)

    rebuild = _start(db, workspace)
    _interleave(db, workspace, world, store_root, rebuild.id, action, inject_at)

    result = _run(db, lambda engine, uow, store: _cut(uow, workspace, rebuild.id), store_root)

    generations = _gens(db, store_root)
    deleted = action == "delete"
    activated = action == "activate"
    expected_live = (
        {world["alpha_version"]} if deleted
        else {world["alpha_version"], world["replacement"]} if activated
        else {world["alpha_version"], world["beta_version"], world["replacement"]}
    )
    activated_versions = {
        row["version"] for row in generations if row["state"] == "active"
    }
    assert activated_versions == expected_live, f"seed {seed} diverged"
    assert result["activated"] >= len(expected_live)


def test_rebuild_service_refuses_unknown_ids_and_empty_workspaces(
    db, workspace, store_root,
) -> None:
    # No rebuild row at all: status is None and coverage is trivially zero.
    assert _run(db, lambda engine, uow, store: rebuild_status(
        uow, workspace_id=workspace["workspace_id"],
    ), store_root) is None
    assert _run(db, lambda engine, uow, store: verify_candidate(
        uow, workspace_id=workspace["workspace_id"],
    ), store_root) == {"live": 0, "covered": 0}

    missing = str(uuid.uuid4())

    with pytest.raises(RebuildError) as excinfo:
        _run(db, lambda engine, uow, store: apply_outbox_event(
            uow, missing, seq=1, topic=ACTIVATED, payload={},
        ), store_root)
    assert excinfo.value.reason == "not_found"

    with pytest.raises(RebuildError) as excinfo:
        _run(db, lambda engine, uow, store: drain_outbox(uow, missing), store_root)
    assert excinfo.value.reason == "not_found"

    with pytest.raises(RebuildError) as excinfo:
        _run(db, lambda engine, uow, store: activate_candidate(
            uow, EventWriter(), workspace_id=workspace["workspace_id"],
            rebuild_id=missing,
        ), store_root)
    assert excinfo.value.reason == "not_found"


def _go_live(db, store_root: Path, version_id: str) -> None:
    async def run(engine, _uow, _store) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE source_versions SET state = 'active' WHERE id = :id"),
                {"id": version_id},
            )

    _run(db, run, store_root)


def _empty_live_version(db, workspace: dict, store_root: Path) -> str:
    """A live member whose object extracts to nothing (whitespace only)."""
    version = _upload(db, workspace, store_root, data=b"   \n  ")
    _go_live(db, store_root, version)
    return version


def test_stage_refuses_member_content_that_extracts_to_nothing(
    db, workspace, store_root,
) -> None:
    empty_version = _empty_live_version(db, workspace, store_root)

    with pytest.raises(IndexingError) as excinfo:
        _stage(db, workspace, store_root, empty_version)
    assert excinfo.value.reason == "empty_content"


def test_worker_fails_rebuild_when_member_content_is_empty(
    db, workspace, store_root,
) -> None:
    from apps.worker.tasks.rebuild_index import rebuild_payload

    _empty_live_version(db, workspace, store_root)
    rebuild = _start(db, workspace)
    job_id = _enqueue_rebuild(db, workspace, rebuild)

    async def deliver(engine, _uow, store) -> str:
        return await rebuild_payload(
            engine, store, NEW_PROFILE, FakeEmbeddingProvider(8),
            {"job_id": job_id, "rebuild_id": rebuild.id,
             "workspace_id": workspace["workspace_id"]},
        )

    assert _run(db, deliver, store_root) == "failed:empty_content"

    async def verify(_engine, uow, _store) -> tuple:
        async with uow.transaction() as transaction:
            row = (
                await transaction.execute(
                    text("SELECT state, detail FROM index_rebuilds WHERE id = :id"),
                    {"id": rebuild.id},
                )
            ).mappings().one()
        return row["state"], row["detail"]

    assert _run(db, verify, store_root) == ("failed", "empty_content")


def test_actor_composes_settings_store_and_pipeline(
    db, workspace, world, store_root, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.worker.tasks import rebuild_index as task_module

    rebuild = _start(db, workspace)
    job = _enqueue_rebuild(db, workspace, rebuild)

    class FakeSettings:
        database_url = db.database_url
        data_dir = str(store_root)
        ollama_host = "http://127.0.0.1:11434"

        @classmethod
        def load(cls) -> "FakeSettings":
            return cls()

        @property
        def active_profile(self) -> ModelProfile:
            return NEW_PROFILE

    monkeypatch.setattr(task_module, "Settings", FakeSettings)
    monkeypatch.setattr(task_module, "ObjectStore", lambda _root: ObjectStore(store_root))
    monkeypatch.setattr(
        task_module, "OllamaEmbeddingProvider",
        lambda *_args: FakeEmbeddingProvider(NEW_PROFILE.embedding_dimensions),
    )

    payload = {"job_id": job, "rebuild_id": rebuild.id,
               "workspace_id": workspace["workspace_id"]}
    assert task_module.rebuild_index(**payload) == "completed"

    generations = _gens(db, store_root)
    assert {row["version"] for row in generations if row["state"] == "active"} == {
        world["alpha_version"], world["beta_version"], world["replacement"],
    }

    # A duplicate delivery of the completed job is claimed by nobody.
    assert task_module.rebuild_index(**payload) == "skipped"
