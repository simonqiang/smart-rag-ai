"""Dependency licence scan: fail on restricted licences (AGPL family).

Covers the Python environment and the npm workspaces. Unknown or missing
licence metadata passes for now; Task 28 tightens this before packaging.
"""

from __future__ import annotations

import json
import subprocess
from importlib import metadata

DENIED = ("AGPL",)


def python_licences() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"] or "?"
        raw = f"{dist.metadata.get('License-Expression', '')} {dist.metadata.get('License', '')}"
        classifiers = " ".join(
            c for c in (dist.metadata.get_all("Classifier") or []) if c.startswith("License")
        )
        found.append((name, f"{raw} {classifiers}"))
    return found


def npm_licences() -> list[tuple[str, str]]:
    raw = subprocess.run(
        ["npm", "ls", "--all", "--json", "--long"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tree = json.loads(raw).get("dependencies", {})

    def walk(node: dict, out: list[tuple[str, str]]) -> None:
        for name, info in node.items():
            licence = info.get("licenses", "")
            if isinstance(licence, list):
                licence = " ".join(str(item) for item in licence)
            out.append((name, str(licence)))
            walk(info.get("dependencies", {}), out)

    found: list[tuple[str, str]] = []
    walk(tree, found)
    return found


def main() -> int:
    violations = [
        f"{name}: {licence}"
        for name, licence in python_licences() + npm_licences()
        if any(marker in str(licence).upper() for marker in DENIED)
    ]
    if violations:
        print(f"licence scan FAILED (denied: {', '.join(DENIED)}):")
        for line in sorted(set(violations)):
            print(f"  {line}")
        return 1
    print("licence scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
