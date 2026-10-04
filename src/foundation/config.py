"""Typed application settings loaded from config/default.yaml.

Task 1 contract: ``Settings.load() -> Settings``. Secrets never live in the
YAML file; they arrive through ``SMART_RAG_*`` environment variables and are
exposed only through :meth:`Settings.secret_values` so diagnostics can redact
them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
_SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


@dataclass(frozen=True)
class ModelProfile:
    name: str
    chat_model: str
    embedding_model: str
    embedding_dimensions: int


@dataclass(frozen=True)
class CrawlLimits:
    max_pages: int
    max_depth: int
    max_minutes: int
    max_response_mb: int


@dataclass(frozen=True)
class RetentionLimits:
    superseded_days: int
    conversation_days: int


@dataclass(frozen=True)
class Limits:
    upload_mb: int
    max_pages_per_document: int
    decompressed_mb: int
    crawl: CrawlLimits
    retention: RetentionLimits


@dataclass(frozen=True)
class Settings:
    profile: str
    profiles: dict[str, ModelProfile]
    limits: Limits
    data_dir: str
    backup_dir: str
    ollama_host: str

    @property
    def active_profile(self) -> ModelProfile:
        return self.profiles[self.profile]

    @classmethod
    def load(cls, path: Path | None = None, overrides: dict | None = None) -> Settings:
        raw = yaml.safe_load((path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
        overrides = overrides or {}
        for key, value in overrides.items():
            if key in ("data_dir", "backup_dir"):
                raw["paths"][key] = value
            elif key in ("ollama_host", "profile"):
                raw[key] = value
            else:
                raise ValueError(f"unsupported override: {key}")
        profile = raw["profile"]
        profiles = {
            name: ModelProfile(name=name, **spec)
            for name, spec in raw["profiles"].items()
        }
        if profile not in profiles:
            raise ValueError(f"unknown profile: {profile!r} (known: {sorted(profiles)})")
        crawl = CrawlLimits(**raw["limits"]["crawl"])
        retention = RetentionLimits(**raw["limits"]["retention"])
        limits = Limits(
            upload_mb=raw["limits"]["upload_mb"],
            max_pages_per_document=raw["limits"]["max_pages_per_document"],
            decompressed_mb=raw["limits"]["decompressed_mb"],
            crawl=crawl,
            retention=retention,
        )
        return cls(
            profile=profile,
            profiles=profiles,
            limits=limits,
            data_dir=raw["paths"]["data_dir"],
            backup_dir=raw["paths"]["backup_dir"],
            ollama_host=raw["ollama_host"],
        )

    def secret_values(self) -> list[str]:
        """Values of SMART_RAG_* environment variables that look sensitive."""
        secrets = []
        for name, value in os.environ.items():
            if name.startswith("SMART_RAG_") and any(m in name for m in _SENSITIVE_MARKERS):
                secrets.append(value)
        return secrets
