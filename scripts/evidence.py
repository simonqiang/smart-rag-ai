"""Write the revision-stamped evidence manifest for a completed gate run.

Usage: python3 scripts/evidence.py <gate> [<gate> ...]
Output: build/evidence-<short-sha>.json
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def main(argv: list[str]) -> int:
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    manifest = {
        "revision": revision,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "result": "PASS",
        "gates": argv[1:],
    }
    output = Path("build") / f"evidence-{manifest['revision']}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
