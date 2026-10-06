"""Deterministic provider fakes for contract and pipeline tests (Task 10).

Fakes return scripted results/errors in order, then fall back to a stable
default. Recorded requests let tests assert what domain code sends.
"""

from __future__ import annotations

import hashlib

from ai_providers.contracts import EmbeddingResult, GenerationRequest, GenerationResult


class FakeGenerationProvider:
    def __init__(
        self,
        script: list[GenerationResult | Exception] | None = None,
        default_text: str = "fake answer",
    ) -> None:
        self.requests: list[GenerationRequest] = []
        self._script = list(script or [])
        self._default_text = default_text

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return GenerationResult(text=self._default_text, model="fake-chat")


def _deterministic_vector(text: str, dimension: int) -> list[float]:
    # sha256 (not hash()): stable across processes and PYTHONHASHSEED values.
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[i % len(digest)] / 255.0 for i in range(dimension)]


class FakeEmbeddingProvider:
    def __init__(self, dimension: int, script: list[EmbeddingResult | Exception] | None = None) -> None:
        self.requests: list[list[str]] = []
        self._dimension = dimension
        self._script = list(script or [])

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.requests.append(list(texts))
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return EmbeddingResult(
            vectors=[_deterministic_vector(text, self._dimension) for text in texts],
            model="fake-embed",
        )
