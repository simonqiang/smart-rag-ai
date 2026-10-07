"""Deterministic chunking of extracted documents (Task 12).

``chunk`` is a pure function: identical documents and policies always
produce identical chunks, for Latin and CJK text alike — boundaries are
character counts, never word guesses, so text without word spaces works the
same way. Each chunk keeps the page and block-ordinal span it was built
from; that span becomes the citation anchor.
"""

from __future__ import annotations

from dataclasses import dataclass

from ingestion.extraction import ExtractedDocument

__all__ = ["Chunk", "ChunkPolicy", "chunk"]


@dataclass(frozen=True)
class ChunkPolicy:
    max_chars: int = 800
    overlap_chars: int = 100
    version: int = 1  # part of the index-generation compatibility key


@dataclass(frozen=True)
class Chunk:
    text: str
    language: str
    page: int | None
    block_start: int
    block_end: int


def chunk(document: ExtractedDocument, policy: ChunkPolicy) -> list[Chunk]:
    pieces = _pieces(document, policy)
    return _pack(pieces, policy)


def _pieces(
    document: ExtractedDocument, policy: ChunkPolicy,
) -> list[tuple[str, str, int | None, int]]:
    """Atomic (text, language, page, block ordinal) units within one block."""
    pieces: list[tuple[str, str, int | None, int]] = []
    for block in document.blocks:
        if len(block.text) <= policy.max_chars:
            pieces.append((block.text, block.language, block.location.page, block.location.block))
            continue
        step = max(policy.max_chars - policy.overlap_chars, 1)
        for start in range(0, len(block.text), step):
            pieces.append((
                block.text[start:start + policy.max_chars],
                block.language, block.location.page, block.location.block,
            ))
    return pieces


def _pack(
    pieces: list[tuple[str, str, int | None, int]], policy: ChunkPolicy,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[tuple[str, str, int | None, int]] = []

    def flush() -> None:
        if not current:
            return
        languages = {language for _, language, _, _ in current}
        chunks.append(Chunk(
            text="\n".join(text for text, _, _, _ in current),
            language=languages.pop() if len(languages) == 1 else "mixed",
            page=current[0][2],
            block_start=current[0][3],
            block_end=current[-1][3],
        ))
        current.clear()

    for piece in pieces:
        if current and (
            piece[2] != current[0][2]  # never pack across pages
            or len("\n".join(text for text, _, _, _ in current)) + len(piece[0]) + 1
            > policy.max_chars
        ):
            flush()
        current.append(piece)
    flush()
    return chunks
