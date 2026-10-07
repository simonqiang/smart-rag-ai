"""Reciprocal-rank fusion (Task 13, spec §15).

The MVP ranking strategy: merge the semantic and keyword rankings by
summing ``1 / (k + rank)`` per item. Ties break deterministically on
``(-score, id)`` so equal scores never reorder between runs. A local
cross-encoder would join here as an additional strategy only if the
evaluation report shows a ranking failure it fixes.
"""

from __future__ import annotations

__all__ = ["RRF_K", "reciprocal_rank_fusion"]

RRF_K = 60


def reciprocal_rank_fusion(
    rankings: list[list[str]], *, k: int = RRF_K
) -> list[tuple[str, float]]:
    """Fuse ranked item IDs into one deterministic ranking with RRF scores."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
