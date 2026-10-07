"""Citation validation (Task 14b, spec §"Citation correctness").

The model marks each claim inline as ``[N:"verbatim quote"]`` where N indexes
the supplied evidence by rank and the quote is copied from that passage.
Validation is purely code-computable: every label must belong to the supplied
evidence and the quoted claim must appear inside the cited span's text
(whitespace/case-insensitive, so copy drift on wrapping never fails a real
quote). Answers without markers are malformed; answers citing labels outside
the evidence are ungrounded. Both are typed and safe to surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from retrieval_answering.retrieval import RankedEvidence

__all__ = [
    "Citation",
    "CitationError",
    "MalformedAnswerError",
    "UngroundedAnswerError",
    "validate_citations",
]

MARKER = re.compile(r"\[(\d+):([^\]]*)\]")


class CitationError(Exception):
    """A generated answer failed citation validation."""

    reason = "citation_error"


class MalformedAnswerError(CitationError):
    reason = "malformed_answer"


class UngroundedAnswerError(CitationError):
    reason = "ungrounded_answer"

    def __init__(self, detail: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(detail)


@dataclass(frozen=True)
class Citation:
    label: int
    chunk_id: str
    source_id: str
    source_name: str
    page: int | None
    block_start: int
    block_end: int
    quote: str

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "page": self.page,
            "block_start": self.block_start,
            "block_end": self.block_end,
            "quote": self.quote,
        }


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def validate_citations(
    answer_text: str, evidence: RankedEvidence
) -> tuple[str, list[Citation]]:
    """Check every marker against the evidence; return display text + citations.

    Display text keeps only the label: ``[N:"quote"]`` becomes ``[N]``.
    """
    markers = MARKER.findall(answer_text)
    if not markers:
        raise MalformedAnswerError("answer carries no citation markers")

    citations: list[Citation] = []
    for label_text, quote in markers:
        label = int(label_text)
        if label < 1 or label > len(evidence.items):
            raise UngroundedAnswerError(
                f"citation [{label}] is outside the supplied evidence",
                reason="invented_citation",
            )
        item = evidence.items[label - 1]
        if not quote.strip() or _normalise(quote) not in _normalise(item.text):
            raise UngroundedAnswerError(
                f"citation [{label}] quotes text outside its evidence span",
                reason="quote_mismatch",
            )
        citations.append(Citation(
            label=label,
            chunk_id=item.chunk_id,
            source_id=item.source_id,
            source_name=item.source_name,
            page=item.page,
            block_start=item.block_start,
            block_end=item.block_end,
            quote=quote,
        ))

    display = MARKER.sub(lambda match: f"[{match.group(1)}]", answer_text)
    return display, citations
