"""Chunk, embed, and stage a source version's index (Task 12).

``index_version`` chunks an extracted document, embeds it through the
provider port, and writes a ``staging`` generation with chunk rows —
embeddings and language-aware keyword vectors — in one transaction. Every
refusal (empty content, unknown version, wrong state, dimension mismatch,
provider failure) happens before any row is written, and re-indexing the
same content is an idempotent no-op, so at-least-once delivery converges.
Activation is a separate, explicit step (``generations.activate_generation``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from ai_providers.contracts import EmbeddingDimensionError, EmbeddingProvider
from foundation.config import ModelProfile
from foundation.unit_of_work import UnitOfWork
from ingestion.chunking import Chunk, ChunkPolicy, chunk
from ingestion.extraction import ExtractedDocument
from knowledge_index.generations import compatibility_key
from knowledge_index.keywords import config_for, segment

__all__ = ["IndexedVersion", "IndexingError", "index_version"]

DEFAULT_POLICY = ChunkPolicy()

REJECTION_MESSAGES = {
    "empty_content": "document has no extractable content to index",
    "version_not_found": "source version does not exist in this workspace",
    "not_extracted": "source version is not in an indexable state",
}


class IndexingError(Exception):
    """Indexing refused; ``reason`` is a stable machine code. Nothing persists."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(REJECTION_MESSAGES.get(reason, reason))


@dataclass(frozen=True)
class IndexedVersion:
    source_version_id: str
    generation_id: str
    compatibility_key: str
    chunk_count: int
    state: str  # 'staging' until activate_generation flips it
    duplicate: bool


async def index_version(
    uow: UnitOfWork,
    embeddings: EmbeddingProvider,
    *,
    workspace_id: str,
    source_version_id: str,
    document: ExtractedDocument,
    profile: ModelProfile,
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> IndexedVersion:
    chunks = chunk(document, policy)
    if not chunks:
        raise IndexingError("empty_content")

    async with uow.transaction() as transaction:
        version = (
            await transaction.execute(
                text("SELECT state FROM source_versions WHERE id = :id AND workspace_id = :ws"),
                {"id": source_version_id, "ws": workspace_id},
            )
        ).first()
        if version is None:
            raise IndexingError("version_not_found")
        if version.state in ("indexed", "active"):
            # 'active' is the live lifecycle state (Task 16); 'indexed' is a
            # ready replacement awaiting cutover. Both mean: already indexed.
            existing = await _existing_generation(transaction, source_version_id, "active")
            return _as_indexed(existing, "active", source_version_id, duplicate=True)
        if version.state != "extracted":
            raise IndexingError("not_extracted")
        staged = await _existing_generation(transaction, source_version_id, "staging")
        if staged is not None:
            # A previous attempt staged but did not activate; reusing it keeps
            # redelivery convergent without re-embedding.
            return _as_indexed(staged, "staging", source_version_id, duplicate=True)

    result = embeddings.embed([chunk.text for chunk in chunks])
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
                "version": source_version_id,
                "key": key,
                "dim": profile.embedding_dimensions,
                "model": profile.embedding_model,
                "count": len(chunks),
            },
        )
        for ordinal, (chunk_row, vector) in enumerate(zip(chunks, result.vectors)):
            await _insert_chunk(transaction, generation_id, workspace_id,
                                source_version_id, ordinal, chunk_row, vector)
    return IndexedVersion(
        source_version_id=source_version_id,
        generation_id=generation_id,
        compatibility_key=key,
        chunk_count=len(chunks),
        state="staging",
        duplicate=False,
    )


async def _existing_generation(transaction, source_version_id: str, state: str):
    return (
        await transaction.execute(
            text(
                "SELECT id, chunk_count, compatibility_key FROM index_generations "
                "WHERE source_version_id = :version AND state = :state"
            ),
            {"version": source_version_id, "state": state},
        )
    ).mappings().first()


def _as_indexed(row, state: str, source_version_id: str, *, duplicate: bool) -> IndexedVersion:
    return IndexedVersion(
        source_version_id=source_version_id,
        generation_id=str(row["id"]),
        compatibility_key=row["compatibility_key"],
        chunk_count=int(row["chunk_count"]),
        state=state,
        duplicate=duplicate,
    )


async def _insert_chunk(transaction, generation_id: str, workspace_id: str,
                        source_version_id: str, ordinal: int,
                        chunk_row: Chunk, vector: list[float]) -> None:
    await transaction.execute(
        text(
            "INSERT INTO index_chunks (id, generation_id, workspace_id, source_id, "
            "source_version_id, ordinal, text, language, page, block_start, block_end, "
            "embedding, keywords) "
            "VALUES (:id, :generation, :ws, "
            "(SELECT source_id FROM source_versions WHERE id = :version), "
            ":version, :ordinal, :text, :language, :page, :block_start, :block_end, "
            "CAST(:embedding AS vector), "
            "to_tsvector(CAST(:config AS regconfig), :keywords))"
        ),
        {
            "id": str(uuid.uuid4()),
            "generation": generation_id,
            "ws": workspace_id,
            "version": source_version_id,
            "ordinal": ordinal,
            "text": chunk_row.text,
            "language": chunk_row.language,
            "page": chunk_row.page,
            "block_start": chunk_row.block_start,
            "block_end": chunk_row.block_end,
            "embedding": _vector_literal(vector),
            "config": config_for(chunk_row.language),
            "keywords": segment(chunk_row.text),
        },
    )


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
