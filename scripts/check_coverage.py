"""Per-module coverage gate: every measured file must reach 96%.

Usage: python3 scripts/check_coverage.py [coverage.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

THRESHOLD = 96.0


def main(argv: list[str]) -> int:
    report_path = Path(argv[1] if len(argv) > 1 else "build/coverage.json")
    report = json.loads(report_path.read_text())
    failures = [
        f"{path}: {data['summary']['percent_covered']:.1f}%"
        for path, data in sorted(report["files"].items())
        if data["summary"]["percent_covered"] < THRESHOLD
    ]
    if failures:
        print(f"coverage gate ({THRESHOLD}% per module) FAILED:")
        for line in failures:
            print(f"  {line}")
        return 1
    print(f"coverage gate ({THRESHOLD}% per module): PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
