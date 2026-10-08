"""Task 13: reciprocal-rank fusion merge, dedupe, and determinism."""

import pytest

from retrieval_answering.ranking import reciprocal_rank_fusion


def test_semantic_and_keyword_rankings_fuse_with_dedupe() -> None:
    fused = reciprocal_rank_fusion([
        ["a", "b", "c"],  # semantic
        ["b", "d"],       # keyword: b hit by both channels, ranked first there
    ])

    ids = [item for item, _score in fused]
    # b appears once (deduped) and rises to the top on combined evidence.
    assert ids[0] == "b"
    assert ids == ["b", "a", "d", "c"]
    assert len(ids) == len(set(ids))

    scores = dict(fused)
    # b: rank 2 in semantic, rank 1 in keyword.
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_ties_break_deterministically_on_id() -> None:
    first = reciprocal_rank_fusion([["x"], ["y"]])
    second = reciprocal_rank_fusion([["y"], ["x"]])
    # Same combined score: the ordering must not depend on channel order.
    assert [i for i, _ in first] == [i for i, _ in second] == ["x", "y"]


def test_single_channel_and_empty_inputs() -> None:
    assert reciprocal_rank_fusion([["a", "b"]])[0] == ("a", 1 / 61)
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
