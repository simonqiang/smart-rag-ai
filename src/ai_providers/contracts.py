"""Provider-neutral AI contracts (Task 10).

Generation and embedding are independent ports: domain code depends only on
these types, and provider SDK/HTTP details stay inside adapters (spec §15).
Every failure is typed with a stable machine ``reason`` code; error messages
carry host, model, status codes, and dimensions — never prompt text or raw
response bodies, so diagnostics are safe to log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    system: str = ""
    temperature: float = 0.0


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str

    @property
    def count(self) -> int:
        return len(self.vectors)

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


class ProviderError(Exception):
    """Typed provider failure; ``reason`` is a stable machine code."""

    reason = "provider_error"

    def __init__(self, detail: str = "", *, reason: str | None = None) -> None:
        self.reason = reason or type(self).reason
        super().__init__(detail or self.reason)


class ProviderUnavailableError(ProviderError):
    """Host is down, refused the connection, or timed out."""

    reason = "unreachable"


class ModelMissingError(ProviderError):
    """The configured model is not pulled on the host."""

    reason = "missing_model"


class InvalidProviderResponseError(ProviderError):
    """The host answered, but the payload is not a valid provider response."""

    reason = "invalid_response"


class EmbeddingDimensionError(ProviderError):
    """Vector dimensions do not match the configured profile."""

    reason = "dimension_mismatch"


class GenerationProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...  # pragma: no branch


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> EmbeddingResult: ...  # pragma: no branch
