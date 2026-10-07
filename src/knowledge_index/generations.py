"""Index generations and the activation pointer (Task 12).

A generation is the index build for one source version under one
compatibility key (embedding model + dimensions + chunk-policy version +
language-configuration version). Chunks are written into a ``staging``
generation first; ``activate_generation`` validates the staged content and
flips the pointers — generation to ``active``, any previous generation of
the version to ``retired``, the version itself to ``indexed`` — inside one
transaction. Retrieval only ever joins active generations, so a failed or
half-finished activation can never leak into results.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from foundation.events import AuditEvent, EventWriter
from foundation.unit_of_work import UnitOfWork
from ingestion.chunking import ChunkPolicy
from knowledge_index.keywords import LANGUAGE_CONFIG_VERSION

__all__ = [
    "ActivationError",
    "IndexGeneration",
    "activate_generation",
    "compatibility_key",
]

ACTIVATION_MESSAGES = {
    "not_found": "index generation does not exist in this workspace",
    "not_staging": "index generation is not staged for activation",
    "empty": "index generation has no staged chunks",
    "dimension_mismatch": "staged vectors do not match the generation dimension",
}


class ActivationError(Exception):
    """Activation refused; ``reason`` is a stable machine code. The
    generation, version, and any previously active pointer are unchanged."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(ACTIVATION_MESSAGES.get(reason, reason))


@dataclass(frozen=True)
class IndexGeneration:
    id: str
    workspace_id: str
    source_version_id: str
    compatibility_key: str
    dimension: int
    model: str
    state: str
    chunk_count: int


def compatibility_key(embedding_model: str, dimensions: int, policy: ChunkPolicy) -> str:
    """Everything that must match for vectors to be interchangeable."""
    return (
        f"{embedding_model}:{dimensions}"
        f":chunk-v{policy.version}:lang-v{LANGUAGE_CONFIG_VERSION}"
    )


def _generation(row) -> IndexGeneration:
    return IndexGeneration(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        source_version_id=str(row["source_version_id"]),
        compatibility_key=row["compatibility_key"],
        dimension=int(row["dimension"]),
        model=row["model"],
        state=row["state"],
        chunk_count=int(row["chunk_count"]),
    )


_FETCH = text(
    "SELECT id, workspace_id, source_version_id, compatibility_key, dimension, "
    "model, state, chunk_count FROM index_generations WHERE id = :id"
)


async def activate_generation(
    uow: UnitOfWork,
    writer: EventWriter,
    generation_id: str,
    *,
    workspace_id: str,
    actor: str = "system",
) -> IndexGeneration:
    async with uow.transaction() as transaction:
        row = (
            await transaction.execute(
                text("SELECT * FROM index_generations WHERE id = :id FOR UPDATE"),
                {"id": generation_id},
            )
        ).mappings().first()
        if row is None or str(row["workspace_id"]) != workspace_id:
            # Unknown and foreign IDs are indistinguishable: IDs cannot be probed.
            raise ActivationError("not_found")
        if row["state"] != "staging":
            raise ActivationError("not_staging")

        validation = (
            await transaction.execute(
                text(
                    "SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE vector_dims(embedding) <> :dim) AS wrong_dims "
                    "FROM index_chunks WHERE generation_id = :id"
                ),
                {"id": generation_id, "dim": int(row["dimension"])},
            )
        ).mappings().one()
        if validation["total"] == 0:
            raise ActivationError("empty")
        if validation["wrong_dims"]:
            raise ActivationError("dimension_mismatch")

        await transaction.execute(
            text(
                "UPDATE index_generations SET state = 'retired' "
                "WHERE source_version_id = :version AND state = 'active' AND id <> :id"
            ),
            {"version": str(row["source_version_id"]), "id": generation_id},
        )
        await transaction.execute(
            text(
                "UPDATE index_generations SET state = 'active', activated_at = now() "
                "WHERE id = :id"
            ),
            {"id": generation_id},
        )
        await transaction.execute(
            text(
                "UPDATE source_versions SET state = 'indexed', updated_at = now() "
                "WHERE id = :version"
            ),
            {"version": str(row["source_version_id"])},
        )
        await writer.record(
            AuditEvent(
                type="source.index_activated",
                actor=actor,
                subject=generation_id,
                metadata={
                    "source_version_id": str(row["source_version_id"]),
                    "compatibility_key": row["compatibility_key"],
                    "chunks": int(validation["total"]),
                },
            ),
            transaction,
        )
        activated = (
            await transaction.execute(_FETCH, {"id": generation_id})
        ).mappings().one()
    return _generation(activated)
