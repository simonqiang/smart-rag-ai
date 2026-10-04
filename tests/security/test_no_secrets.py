"""Security smoke (Task 3): committed configuration declares no secret keys."""

from pathlib import Path

import yaml

from foundation.config import _SENSITIVE_MARKERS

ROOT = Path(__file__).resolve().parents[2]


def test_default_config_has_no_secret_like_keys() -> None:
    config = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert not any(marker in key.upper() for marker in _SENSITIVE_MARKERS), (
                    f"secret-like key in committed config: {key}"
                )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config)
