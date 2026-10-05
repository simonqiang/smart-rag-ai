"""Performance budget gate: the production web bundle must stay small.

Blocks when the gzip size of the built JavaScript exceeds the budget. Real
latency budgets land with Task 27 (plan §11 targets); this keeps bundle
growth visible from day one.

Usage: python3 scripts/perf_budget.py [dist-assets-dir]
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

BUDGET_BYTES = 300_000


def main(argv: list[str]) -> int:
    assets = Path(argv[1] if len(argv) > 1 else "apps/web/dist/assets")
    js_files = sorted(assets.glob("*.js"))
    if not js_files:
        print(f"perf budget: no built JS found under {assets}; run make build first")
        return 1
    total = sum(len(gzip.compress(path.read_bytes())) for path in js_files)
    print(f"perf budget: {total} gzip bytes across {len(js_files)} JS files (budget {BUDGET_BYTES})")
    if total > BUDGET_BYTES:
        print("perf budget: FAILED")
        return 1
    print("perf budget: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
