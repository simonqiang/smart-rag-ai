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
# Local Compose topology (loopback-only, nonstandard host ports). Containers
# receive the service-name equivalents through SMART_RAG_* environment vars.
DEFAULT_DATABASE_URL = "postgresql+asyncpg://smart_rag:smart-rag-local@127.0.0.1:54329/smart_rag"
DEFAULT_REDIS_URL = "redis://127.0.0.1:63899/0"


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
    database_url: str
    redis_url: str

    @property
    def active_profile(self) -> ModelProfile:
        return self.profiles[self.profile]

    @classmethod
    def load(cls, path: Path | None = None, overrides: dict | None = None) -> Settings:
        raw = yaml.safe_load((path or DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
        env = os.environ
        if value := env.get("SMART_RAG_PROFILE"):
            raw["profile"] = value
        if value := env.get("SMART_RAG_OLLAMA_HOST"):
            raw["ollama_host"] = value
        if value := env.get("SMART_RAG_DATA_DIR"):
            raw["paths"]["data_dir"] = value
        if value := env.get("SMART_RAG_BACKUP_DIR"):
            raw["paths"]["backup_dir"] = value
        database_url = env.get("SMART_RAG_DATABASE_URL", DEFAULT_DATABASE_URL)
        redis_url = env.get("SMART_RAG_REDIS_URL", DEFAULT_REDIS_URL)
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
            database_url=database_url,
            redis_url=redis_url,
        )

    def secret_values(self) -> list[str]:
        """Values of SMART_RAG_* environment variables that look sensitive."""
        secrets = []
        for name, value in os.environ.items():
            if name.startswith("SMART_RAG_") and any(m in name for m in _SENSITIVE_MARKERS):
                secrets.append(value)
        # Connection strings may carry passwords even though the variable name
        # itself does not match a sensitive marker.
        for url in (self.database_url, self.redis_url):
            password = _url_password(url)
            if password:
                secrets.append(password)
        return secrets


def _url_password(url: str) -> str:
    """Password component of a connection URL, or "" when absent."""
    from urllib.parse import urlparse

    return urlparse(url).password or ""
