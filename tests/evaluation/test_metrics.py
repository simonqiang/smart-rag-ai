"""Task 2: versioned RAG evaluation contract.

Metric math, threshold boundaries, per-language strata, leakage, grounded-
answer labelling, and the versioned baseline/below-profile fixtures.
"""

import json
from pathlib import Path

import pytest

from retrieval_answering.evaluation import (
    CASE_CLASSES,
    LANGUAGES,
    THRESHOLDS,
    EvaluationCase,
    EvaluationResult,
    evaluate,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "evaluation"


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case(cid, language="en", cls="positive", expected=("p1",), forbidden=(), answerable=True):
    return EvaluationCase(
        case_id=cid,
        case_class=cls,
        language=language,
        question=f"question {cid}?",
        expected_passage_ids=list(expected),
        forbidden_passage_ids=list(forbidden),
        expected_answerable=answerable,
    )


def result(cid, ranked=(), cited=(), insufficient=False, grounded=None):
    return EvaluationResult(
        case_id=cid,
        ranked_passage_ids=list(ranked),
        cited_passage_ids=list(cited),
        declared_insufficient=insufficient,
        grounded_accepted=grounded,
    )


def stratum(report, name):
    return report.strata[name]


# --- truth set shape --------------------------------------------------------


def test_baseline_covers_every_case_class_in_every_language():
    cases = [EvaluationCase(**raw) for raw in load_jsonl(FIXTURES / "baseline.jsonl")]
    combos = {(c.language, c.case_class) for c in cases}
    assert combos == {(lang, cls) for lang in LANGUAGES for cls in CASE_CLASSES}


def test_baseline_perfect_results_pass_all_thresholds():
    cases = [EvaluationCase(**raw) for raw in load_jsonl(FIXTURES / "baseline.jsonl")]
    results = [EvaluationResult(**raw) for raw in load_jsonl(FIXTURES / "baseline_results.jsonl")]
    report = evaluate(cases, results)
    assert report.passed
    assert json.loads(report.to_json())["strata"]["aggregate"]["case_count"] == 25


def test_below_profile_fixture_fails():
    cases = [EvaluationCase(**raw) for raw in load_jsonl(FIXTURES / "baseline.jsonl")]
    results = [EvaluationResult(**raw) for raw in load_jsonl(FIXTURES / "below_profile_results.jsonl")]
    report = evaluate(cases, results)
    assert not report.passed
    assert stratum(report, "zh-Hant").recall_at_10 == 0.0  # retrieval lost
    assert stratum(report, "en").leakage_count >= 1  # superseded passage leaked
    assert stratum(report, "ms").leakage_count >= 1  # restricted passage leaked


# --- retrieval metrics ------------------------------------------------------


def test_recall_is_fraction_of_expected_passages_found_in_top10():
    cases = [case("c1", expected=["a", "b"])]
    results = [result("c1", ranked=["b", "x", "a"])]  # both found within 10
    report = evaluate(cases, results)
    assert stratum(report, "en").recall_at_10 == 1.0
    results[0].ranked_passage_ids = ["b", "x"]  # one of two found
    report = evaluate(cases, results)
    assert stratum(report, "en").recall_at_10 == pytest.approx(0.5)


def test_mrr_scores_first_relevant_rank():
    cases = [case("c1"), case("c2"), case("c3")]
    results = [
        result("c1", ranked=["p1", "x"]),  # rank 1 -> 1.0
        result("c2", ranked=["x", "y", "p1"]),  # rank 3 -> 1/3
        result("c3", ranked=["x"] + [f"d{i}" for i in range(10)] + ["p1"]),  # rank 12 -> 0
    ]
    report = evaluate(cases, results)
    assert stratum(report, "en").mrr_at_10 == pytest.approx((1.0 + 1 / 3 + 0.0) / 3)


def test_passage_at_rank_11_does_not_count_for_recall():
    cases = [case("c1", expected=["a"])]
    results = [result("c1", ranked=[f"d{i}" for i in range(10)] + ["a"])]
    report = evaluate(cases, results)
    assert stratum(report, "en").recall_at_10 == 0.0
    assert stratum(report, "en").mrr_at_10 == 0.0


# --- threshold boundaries ---------------------------------------------------


def test_recall_boundary_exactly_at_threshold_passes_and_below_fails():
    # 10 cases x 1 relevant passage each: 9 hits = 0.90 (pass), 8 hits = fail.
    def build(hits):
        cs = [case(f"c{i}", expected=[f"p{i}"]) for i in range(10)]
        rs = [
            result(f"c{i}", ranked=[f"p{i}"] if i < hits else ["miss"])
            for i in range(10)
        ]
        return evaluate(cs, rs)

    assert stratum(build(9), "en").recall_at_10 == pytest.approx(THRESHOLDS["recall_at_10"])
    assert build(9).passed
    assert not build(8).passed


def test_mrr_boundary_exactly_at_threshold():
    # Ranks 1, 1, 2, 2 -> (1 + 1 + 0.5 + 0.5)/4 = 0.75 exactly: pass.
    cs = [case(f"c{i}") for i in range(4)]
    rs = [
        result("c0", ranked=["p1"]),
        result("c1", ranked=["p1"]),
        result("c2", ranked=["x", "p1"]),
        result("c3", ranked=["x", "p1"]),
    ]
    report = evaluate(cs, rs)
    assert stratum(report, "en").mrr_at_10 == pytest.approx(THRESHOLDS["mrr_at_10"])
    assert report.passed
    rs[3].ranked_passage_ids = ["x", "y", "z", "w", "p1"]  # rank 5 -> 0.2
    report = evaluate(cs, rs)
    assert stratum(report, "en").mrr_at_10 == pytest.approx(0.675)
    assert not report.passed


def test_insufficient_precision_and_recall_boundaries():
    # 10 insufficient + 10 answerable cases. TP=9 (refused correctly),
    # FP=1 (refused an answerable case) -> precision 0.90 exactly: pass.
    cs = [case(f"n{i}", cls="insufficient_evidence", expected=[], answerable=False) for i in range(9)]
    cs += [case("n-amb", cls="ambiguous", expected=[], answerable=False)]
    cs += [case(f"y{i}", expected=[f"p{i}"]) for i in range(10)]
    rs = [result(c.case_id, insufficient=True) for c in cs[:9]]
    rs += [result("n-amb", insufficient=False, cited=[])]  # ambiguous answered: FN
    rs += [result(f"y{i}", ranked=[f"p{i}"], cited=[f"p{i}"], insufficient=(i == 9)) for i in range(10)]
    report = evaluate(cs, rs)
    s = stratum(report, "en")
    assert s.insufficient_recall == pytest.approx(0.90)  # 9 TP of 10 refusable
    assert s.insufficient_precision == pytest.approx(0.90)  # 9 TP, 1 FP
    assert report.passed
    # One more wrongful refusal: precision drops to 9/11 < 0.90 -> fail.
    rs[10].declared_insufficient = True
    report = evaluate(cs, rs)
    assert stratum(report, "en").insufficient_precision < THRESHOLDS["insufficient_precision"]
    assert not report.passed


# --- citations and grounding ------------------------------------------------


def test_citation_correctness_counts_cited_cases_only():
    cases = [case("c1", expected=["a"]), case("c2", cls="insufficient_evidence", expected=[], answerable=False)]
    results = [
        result("c1", ranked=["a", "b"], cited=["a"]),  # cites evidence that supports
        result("c2", insufficient=True, cited=[]),  # refusal: not a cited case
    ]
    report = evaluate(cases, results)
    assert stratum(report, "en").citation_correctness == 1.0
    results[0].cited_passage_ids = ["zzz"]  # invented citation: not in evidence
    report = evaluate(cases, results)
    assert stratum(report, "en").citation_correctness == 0.0
    assert not report.passed


def test_citation_boundary_exactly_98_percent():
    cases = [case(f"c{i}", expected=["a"]) for i in range(50)]
    results = [
        result(f"c{i}", ranked=["a"], cited=["a" if i < 49 else "wrong"])
        for i in range(50)
    ]
    report = evaluate(cases, results)
    assert stratum(report, "en").citation_correctness == pytest.approx(THRESHOLDS["citation_correctness"])
    assert report.passed
    results[48].cited_passage_ids = ["also-wrong"]
    assert not evaluate(cases, results).passed


def test_grounded_acceptance_uses_human_labels_only():
    cases = [case(f"c{i}", expected=["a"]) for i in range(20)]
    results = [result(f"c{i}", ranked=["a"], cited=["a"], grounded=(i < 19)) for i in range(20)]
    report = evaluate(cases, results)
    assert stratum(report, "en").grounded_acceptance == pytest.approx(0.95)
    assert report.passed
    results[18].grounded_accepted = False  # 18/20 = 0.90 < 0.95
    report = evaluate(cases, results)
    assert not report.passed


def test_grounded_acceptance_pending_when_unlabelled():
    cases = [case("c1", expected=["a"])]
    results = [result("c1", ranked=["a"], cited=["a"], grounded=None)]
    report = evaluate(cases, results)
    assert stratum(report, "en").grounded_acceptance is None
    assert report.passed  # unlabelled is pending, not failing


# --- leakage and per-language independence ----------------------------------


def test_leakage_must_be_exactly_zero():
    cases = [
        case("stale", cls="stale_version", expected=["v2"], forbidden=["v1"]),
        case("unauth", cls="unauthorized", expected=[], forbidden=["secret"], answerable=False),
    ]
    results = [
        result("stale", ranked=["v2"], cited=["v2"]),
        result("unauth", insufficient=True),
    ]
    report = evaluate(cases, results)
    assert report.passed
    results[0].ranked_passage_ids = ["v1", "v2"]
    report = evaluate(cases, results)
    assert stratum(report, "en").leakage_count == 1
    assert not report.passed  # exactly-zero rule


def test_failing_stratum_fails_report_even_when_aggregate_passes():
    # One zh-Hant case below threshold; enough perfect en/mixed cases to keep
    # the aggregate above every threshold.
    cases = [case(f"e{i}") for i in range(20)]
    cases += [case("zh-bad", language="zh-Hant", expected=["missed"])]
    cases += [case("mx", language="mixed", expected=["a"])]
    results = [result(f"e{i}", ranked=["p1"], cited=["p1"]) for i in range(20)]
    results += [result("zh-bad", ranked=["wrong"], cited=[])]
    results += [result("mx", ranked=["a"], cited=["a"])]
    report = evaluate(cases, results)
    aggregate = report.strata["aggregate"]
    assert aggregate.recall_at_10 >= THRESHOLDS["recall_at_10"]  # aggregate passes on its own
    assert stratum(report, "zh-Hant").recall_at_10 == 0.0
    assert stratum(report, "zh-Hant").failures
    assert not report.passed


def test_unknown_stratum_or_class_is_rejected():
    with pytest.raises(ValueError):
        evaluate([case("c1", language="klingon")], [result("c1", ranked=["p1"])])
    with pytest.raises(ValueError):
        evaluate([case("c1", cls="mystery")], [result("c1", ranked=["p1"])])


def test_every_case_must_have_exactly_one_result():
    cases = [case("c1")]
    with pytest.raises(ValueError, match="c1"):
        evaluate(cases, [])
    with pytest.raises(ValueError, match="c1"):
        evaluate(cases, [result("c1", ranked=["p1"]), result("c1", ranked=["p1"])])


# --- machine-readable report ------------------------------------------------


def test_report_to_json_is_deterministic_and_machine_readable():
    cases = [EvaluationCase(**raw) for raw in load_jsonl(FIXTURES / "baseline.jsonl")]
    results = [EvaluationResult(**raw) for raw in load_jsonl(FIXTURES / "baseline_results.jsonl")]
    first = evaluate(cases, results).to_json()
    second = evaluate(cases, results).to_json()
    assert first == second  # deterministic
    payload = json.loads(first)
    assert set(payload["strata"]) == {*LANGUAGES, "aggregate"}
    for name in ("recall_at_10", "mrr_at_10", "citation_correctness", "grounded_acceptance",
                 "insufficient_precision", "insufficient_recall", "leakage_count"):
        assert name in payload["strata"]["aggregate"]
    assert payload["thresholds"] == THRESHOLDS


def test_evaluation_cli_exit_codes(tmp_path):
    from retrieval_answering.evaluation import main

    out = tmp_path / "report.json"
    rc = main([
        "--baseline", str(FIXTURES / "baseline.jsonl"),
        "--results", str(FIXTURES / "baseline_results.jsonl"),
        "--output", str(out),
    ])
    assert rc == 0
    assert json.loads(out.read_text())["passed"] is True
    rc = main([
        "--baseline", str(FIXTURES / "baseline.jsonl"),
        "--results", str(FIXTURES / "below_profile_results.jsonl"),
        "--output", str(tmp_path / "bad.json"),
    ])
    assert rc == 1
