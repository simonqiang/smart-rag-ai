"""Versioned RAG evaluation contract (Task 2).

Computes the blocking metrics from the testing strategy §7 over a versioned
truth set. Retrieval, leakage, insufficient-evidence, and citation metrics are
calculated by code; grounded-answer acceptance is read from human-labelled
judgments carried on the results (an LLM judge may only be advisory and is out
of scope here).

Every language stratum must independently meet the thresholds; the aggregate
can never hide a failing stratum.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

LANGUAGES = ("en", "zh-Hans", "zh-Hant", "ms", "mixed")
CASE_CLASSES = ("positive", "insufficient_evidence", "ambiguous", "stale_version", "unauthorized")
TOP_K = 10

THRESHOLDS = {
    "recall_at_10": 0.90,
    "mrr_at_10": 0.75,
    "citation_correctness": 0.98,
    "grounded_acceptance": 0.95,
    "insufficient_precision": 0.90,
    "insufficient_recall": 0.85,
    "leakage": 0.0,  # exact-zero rule: any stale or unauthorized hit fails
}


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    case_class: str
    language: str
    question: str
    expected_passage_ids: list[str]
    forbidden_passage_ids: list[str] = field(default_factory=list)
    expected_answerable: bool = True


@dataclass
class EvaluationResult:
    """Mutable by design: results are working records the pipeline updates."""

    case_id: str
    ranked_passage_ids: list[str]
    cited_passage_ids: list[str] = field(default_factory=list)
    declared_insufficient: bool = False
    grounded_accepted: bool | None = None  # human label; None = not yet labelled


@dataclass
class StratumReport:
    stratum: str
    case_count: int
    recall_at_10: float | None
    mrr_at_10: float | None
    citation_correctness: float | None
    grounded_acceptance: float | None
    insufficient_precision: float | None
    insufficient_recall: float | None
    leakage_count: int
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for rank, passage_id in enumerate(ranked[:TOP_K], start=1):
        if passage_id in relevant:
            return 1.0 / rank
    return 0.0


def _stratum_report(stratum: str, pairs: list[tuple[EvaluationCase, EvaluationResult]]) -> StratumReport:
    if not pairs:
        # An empty stratum has nothing to evaluate: pending, not failing.
        return StratumReport(
            stratum=stratum, case_count=0, recall_at_10=None, mrr_at_10=None,
            citation_correctness=None, grounded_acceptance=None,
            insufficient_precision=None, insufficient_recall=None, leakage_count=0,
        )
    answerable = [(c, r) for c, r in pairs if c.expected_answerable]
    refusable = [(c, r) for c, r in pairs if not c.expected_answerable]

    recalls, rrs = [], []
    for c, r in answerable:
        relevant = set(c.expected_passage_ids)
        if relevant:
            top = set(r.ranked_passage_ids[:TOP_K])
            recalls.append(len(top & relevant) / len(relevant))
        rrs.append(_reciprocal_rank(r.ranked_passage_ids, relevant))

    cited = [(c, r) for c, r in pairs if r.cited_passage_ids]
    correct_citations = sum(
        1
        for c, r in cited
        if set(r.cited_passage_ids) <= set(r.ranked_passage_ids[:TOP_K])
        and set(r.cited_passage_ids) & set(c.expected_passage_ids)
    )

    labelled = [
        (c, r)
        for c, r in answerable
        if not r.declared_insufficient and r.grounded_accepted is not None
    ]
    tp = sum(1 for _, r in refusable if r.declared_insufficient)
    fp = sum(1 for _, r in answerable if r.declared_insufficient)
    fn = len(refusable) - tp

    leakage_count = 0
    for c, r in pairs:
        if c.case_class in ("stale_version", "unauthorized"):
            if set(c.forbidden_passage_ids) & set(r.ranked_passage_ids[:TOP_K]):
                leakage_count += 1

    report = StratumReport(
        stratum=stratum,
        case_count=len(pairs),
        recall_at_10=sum(recalls) / len(recalls) if recalls else 0.0,
        mrr_at_10=sum(rrs) / len(rrs) if rrs else 0.0,
        citation_correctness=correct_citations / len(cited) if cited else None,
        grounded_acceptance=(
            sum(1 for _, r in labelled if r.grounded_accepted) / len(labelled) if labelled else None
        ),
        insufficient_precision=tp / (tp + fp) if (tp + fp) else None,
        insufficient_recall=tp / (tp + fn) if (tp + fn) else None,
        leakage_count=leakage_count,
    )
    report.failures = _threshold_failures(report)
    return report


def _threshold_failures(s: StratumReport) -> list[str]:
    failures = []
    if s.recall_at_10 is not None and s.recall_at_10 < THRESHOLDS["recall_at_10"]:
        failures.append(f"recall_at_10={s.recall_at_10:.4f} < {THRESHOLDS['recall_at_10']}")
    if s.mrr_at_10 is not None and s.mrr_at_10 < THRESHOLDS["mrr_at_10"]:
        failures.append(f"mrr_at_10={s.mrr_at_10:.4f} < {THRESHOLDS['mrr_at_10']}")
    if s.citation_correctness is not None and s.citation_correctness < THRESHOLDS["citation_correctness"]:
        failures.append(f"citation_correctness={s.citation_correctness:.4f} < {THRESHOLDS['citation_correctness']}")
    if s.grounded_acceptance is not None and s.grounded_acceptance < THRESHOLDS["grounded_acceptance"]:
        failures.append(f"grounded_acceptance={s.grounded_acceptance:.4f} < {THRESHOLDS['grounded_acceptance']}")
    if s.insufficient_precision is not None and s.insufficient_precision < THRESHOLDS["insufficient_precision"]:
        failures.append(f"insufficient_precision={s.insufficient_precision:.4f} < {THRESHOLDS['insufficient_precision']}")
    if s.insufficient_recall is not None and s.insufficient_recall < THRESHOLDS["insufficient_recall"]:
        failures.append(f"insufficient_recall={s.insufficient_recall:.4f} < {THRESHOLDS['insufficient_recall']}")
    if s.leakage_count > THRESHOLDS["leakage"]:
        failures.append(f"leakage_count={s.leakage_count} > 0 (exact-zero rule)")
    return sorted(failures)


@dataclass
class EvaluationReport:
    strata: dict[str, StratumReport]
    thresholds: dict[str, float] = field(default_factory=lambda: dict(THRESHOLDS))

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.strata.values())

    def to_json(self) -> str:
        payload = {
            "passed": self.passed,
            "thresholds": self.thresholds,
            "strata": {
                name: {
                    "stratum": s.stratum,
                    "case_count": s.case_count,
                    "recall_at_10": s.recall_at_10,
                    "mrr_at_10": s.mrr_at_10,
                    "citation_correctness": s.citation_correctness,
                    "grounded_acceptance": s.grounded_acceptance,
                    "insufficient_precision": s.insufficient_precision,
                    "insufficient_recall": s.insufficient_recall,
                    "leakage_count": s.leakage_count,
                    "failures": s.failures,
                    "passed": s.passed,
                }
                for name, s in self.strata.items()
            },
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def evaluate(
    cases: list[EvaluationCase], results: list[EvaluationResult]
) -> EvaluationReport:
    by_id: dict[str, EvaluationResult] = {}
    for r in results:
        if r.case_id in by_id:
            raise ValueError(f"duplicate result for case {r.case_id!r}")
        by_id[r.case_id] = r

    for c in cases:
        if c.language not in LANGUAGES:
            raise ValueError(f"unknown language stratum: {c.language!r}")
        if c.case_class not in CASE_CLASSES:
            raise ValueError(f"unknown case class: {c.case_class!r}")
        if c.case_id not in by_id:
            raise ValueError(f"no result for case {c.case_id!r}")

    pairs = [(c, by_id[c.case_id]) for c in cases]
    strata: dict[str, StratumReport] = {
        lang: _stratum_report(lang, [(c, r) for c, r in pairs if c.language == lang])
        for lang in LANGUAGES
    }
    strata["aggregate"] = _stratum_report("aggregate", pairs)
    return EvaluationReport(strata=strata)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAG results against the truth set")
    parser.add_argument("--baseline", required=True, help="path to the versioned truth set (JSONL)")
    parser.add_argument("--results", required=True, help="path to pipeline results (JSONL)")
    parser.add_argument("--output", required=True, help="path to write the machine-readable report")
    args = parser.parse_args(argv)

    cases = [EvaluationCase(**json.loads(line)) for line in Path(args.baseline).read_text(encoding="utf-8").splitlines() if line.strip()]
    results = [EvaluationResult(**json.loads(line)) for line in Path(args.results).read_text(encoding="utf-8").splitlines() if line.strip()]
    report = evaluate(cases, results)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_json() + "\n", encoding="utf-8")
    print(f"evaluation: {'PASS' if report.passed else 'FAIL'} -> {args.output}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
