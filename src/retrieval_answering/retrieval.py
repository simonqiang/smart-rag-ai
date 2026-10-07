"""Authorized hybrid retrieval (Task 13).

Semantic (pgvector cosine) and keyword (language-aware PostgreSQL full-text)
searches run under one database-side authorization filter — workspace,
active generation, indexed version, active source, and the caller's
collection grants — so a row the context may not see never leaves
PostgreSQL. Results merge through reciprocal-rank fusion; each item carries
both channel ranks and its citation location.

# ponytail: sequential scans on pgvector/tsvector — fine at MVP corpus size,
# add HNSW/GIN tuning when a real corpus makes retrieval p95 matter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy import bindparam, text

from ai_providers.contracts import EmbeddingDimensionError, EmbeddingProvider
from foundation.unit_of_work import UnitOfWork
from identity_access.authorization import AccessContext
from ingestion.extraction import detect_language
from knowledge_index.keywords import config_for, segment
from retrieval_answering.ranking import reciprocal_rank_fusion

__all__ = [
    "CONFIDENCE_THRESHOLD",
    "EmptyQueryError",
    "Evidence",
    "RankedEvidence",
    "retrieve",
]

CANDIDATE_POOL = 20  # per-channel candidates before fusion
DEFAULT_LIMIT = 10
# RRF floor for "worth answering from": a chunk found by both channels at
# rank 1 scores ~0.033; single-channel rank-1 tops out at 1/61 ≈ 0.016.
CONFIDENCE_THRESHOLD = 0.02


class EmptyQueryError(Exception):
    """The question carries no retrievable text."""


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    source_id: str
    source_name: str
    text: str
    language: str
    page: int | None
    block_start: int
    block_end: int
    rank: int
    semantic_rank: int | None
    keyword_rank: int | None
    score: float


@dataclass(frozen=True)
class RankedEvidence:
    query_language: str
    items: list[Evidence] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        return bool(self.items) and self.items[0].score >= CONFIDENCE_THRESHOLD

    def to_json(self) -> list[dict]:
        return [asdict(item) for item in self.items]


def _auth_filter(*, source_id: bool) -> str:
    return (
        "FROM index_chunks c "
        "JOIN index_generations g ON g.id = c.generation_id AND g.state = 'active' "
        "JOIN source_versions v ON v.id = c.source_version_id AND v.state = 'indexed' "
        "JOIN sources s ON s.id = c.source_id AND s.state = 'active' "
        "WHERE c.workspace_id = :workspace_id "
        "AND s.collection_id IN :grants "
        + ("AND s.id = CAST(:source_id AS uuid) " if source_id else "")
    )


async def _channel_ids(
    transaction, sql, parameters: dict
) -> list[str]:
    # asyncpg decodes uuid columns to uuid.UUID; ranks key on the string form.
    return [str(value) for value in (await transaction.execute(sql, parameters)).scalars()]


async def retrieve(
    uow: UnitOfWork,
    embeddings: EmbeddingProvider,
    *,
    context: AccessContext,
    query: str,
    profile,
    limit: int = DEFAULT_LIMIT,
    source_id: str | None = None,
) -> RankedEvidence:
    if not query.strip():
        raise EmptyQueryError("query is empty")
    if not context.collection_ids:
        # No grants (or unknown/disabled user): authorization fails closed.
        return RankedEvidence(query_language=detect_language(query))

    source_filter = bool(source_id)
    semantic_sql = text(
        f"SELECT c.id {_auth_filter(source_id=source_filter)} "
        "ORDER BY c.embedding <=> CAST(:query_vector AS vector) LIMIT :pool"
    ).bindparams(
        bindparam("grants", expanding=True),
        *([bindparam("source_id")] if source_filter else []),
    )
    query_text = segment(query)
    keyword_sql = text(
        f"SELECT c.id {_auth_filter(source_id=source_filter)} "
        "AND c.keywords @@ plainto_tsquery(CAST(:config AS regconfig), :query_text) "
        "ORDER BY ts_rank(c.keywords, "
        "plainto_tsquery(CAST(:config AS regconfig), :query_text)) DESC LIMIT :pool"
    ).bindparams(
        bindparam("grants", expanding=True),
        *([bindparam("source_id")] if source_filter else []),
    )
    base_parameters = {
        "workspace_id": context.workspace_id,
        "grants": context.collection_ids,
        **({"source_id": source_id} if source_filter else {}),
    }

    async with uow.transaction() as transaction:
        embedding = embeddings.embed([query])
        if embedding.dimensions != profile.embedding_dimensions:
            raise EmbeddingDimensionError(
                f"query embedding has {embedding.dimensions} dimensions, "
                f"profile expects {profile.embedding_dimensions}"
            )
        semantic_ids = await _channel_ids(transaction, semantic_sql, {
            **base_parameters,
            "query_vector": _vector_literal(embedding.vectors[0]),
            "pool": CANDIDATE_POOL,
        })
        keyword_ids = await _channel_ids(transaction, keyword_sql, {
            **base_parameters,
            "config": config_for(detect_language(query)),
            "query_text": query_text,
            "pool": CANDIDATE_POOL,
        })

        fused = reciprocal_rank_fusion([semantic_ids, keyword_ids])[:limit]
        if not fused:
            return RankedEvidence(query_language=detect_language(query))
        fused_ids = [chunk_id for chunk_id, _score in fused]
        detail_sql = text(
            f"SELECT c.id AS chunk_id, c.text, c.language, c.page, "
            f"c.block_start, c.block_end, s.id AS source_id, s.name AS source_name "
            f"{_auth_filter(source_id=source_filter)}"
            "AND c.id IN :chunk_ids"
        ).bindparams(
            bindparam("grants", expanding=True),
            bindparam("chunk_ids", expanding=True),
            *([bindparam("source_id")] if source_filter else []),
        )
        details = (
            await transaction.execute(detail_sql, {
                **base_parameters,
                "chunk_ids": fused_ids,
            })
        ).mappings().all()

    return _assemble(detect_language(query), fused, semantic_ids, keyword_ids, details)


def _assemble(
    query_language: str,
    fused: list[tuple[str, float]],
    semantic_ids: list[str],
    keyword_ids: list[str],
    details,
) -> RankedEvidence:
    by_id = {str(row["chunk_id"]): row for row in details}
    semantic_rank = {chunk_id: rank for rank, chunk_id in enumerate(semantic_ids, start=1)}
    keyword_rank = {chunk_id: rank for rank, chunk_id in enumerate(keyword_ids, start=1)}
    items = [
        Evidence(
            chunk_id=str(by_id[chunk_id]["chunk_id"]),
            source_id=str(by_id[chunk_id]["source_id"]),
            source_name=by_id[chunk_id]["source_name"],
            text=by_id[chunk_id]["text"],
            language=by_id[chunk_id]["language"],
            page=by_id[chunk_id]["page"],
            block_start=int(by_id[chunk_id]["block_start"]),
            block_end=int(by_id[chunk_id]["block_end"]),
            rank=rank,
            semantic_rank=semantic_rank.get(chunk_id),
            keyword_rank=keyword_rank.get(chunk_id),
            score=score,
        )
        for rank, (chunk_id, score) in enumerate(fused, start=1)
        if chunk_id in by_id
    ]
    return RankedEvidence(query_language=query_language, items=items)


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
