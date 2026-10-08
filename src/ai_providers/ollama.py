"""Ollama HTTP adapters for the provider contracts (Task 10).

Stdlib ``urllib`` (same choice as the doctor probe) keeps the runtime
dependency set unchanged; sync calls match the Dramatiq worker processes.
``urlopen`` timeouts raise ``TimeoutError`` (or ``URLError`` wrapping one);
both map to ``ProviderUnavailableError`` with reason ``timeout``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.client import HTTPException

from ai_providers.contracts import (
    EmbeddingDimensionError,
    EmbeddingResult,
    GenerationRequest,
    GenerationResult,
    InvalidProviderResponseError,
    ModelMissingError,
    ProviderUnavailableError,
)

DEFAULT_TIMEOUT_SECONDS = 300.0


class _OllamaHttp:
    def __init__(self, host: str, timeout: float) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout

    def _request(self, path: str, payload: dict) -> urllib.request.Request:
        return urllib.request.Request(
            f"{self._host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _open(self, request: urllib.request.Request, path: str, model: str):
        try:
            return urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ModelMissingError(
                    f"model {model!r} not found on {self._host}; run: ollama pull {model}"
                ) from exc
            # Body is deliberately dropped: server errors may echo request data.
            raise ProviderUnavailableError(f"{self._host}{path} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = "timeout" if isinstance(exc.reason, TimeoutError) else "unreachable"
            raise ProviderUnavailableError(
                f"Ollama not reachable at {self._host}", reason=reason
            ) from exc
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Ollama timed out after {self._timeout:g}s at {self._host}", reason="timeout"
            ) from exc
        except OSError as exc:
            raise ProviderUnavailableError(f"Ollama not reachable at {self._host}") from exc

    def post(self, path: str, payload: dict, model: str) -> dict:
        try:
            with self._open(self._request(path, payload), path, model) as response:
                body = response.read()
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Ollama timed out after {self._timeout:g}s at {self._host}", reason="timeout"
            ) from exc
        except OSError as exc:
            raise ProviderUnavailableError(f"Ollama not reachable at {self._host}") from exc
        except HTTPException as exc:
            # e.g. IncompleteRead: the host died before the full response arrived.
            raise ProviderUnavailableError(
                f"connection lost before the full response from {self._host}{path}"
            ) from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise InvalidProviderResponseError(f"non-JSON response from {self._host}{path}") from exc

    def stream(self, path: str, payload: dict, model: str) -> Iterator[dict]:
        """NDJSON line stream; errors during reads keep the same typed mapping."""
        response = self._open(self._request(path, payload), path, model)
        try:
            with response:
                for line in response:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError as exc:
                        raise InvalidProviderResponseError(
                            f"non-JSON line in stream from {self._host}{path}"
                        ) from exc
        except TimeoutError as exc:
            raise ProviderUnavailableError(
                f"Ollama timed out after {self._timeout:g}s at {self._host}", reason="timeout"
            ) from exc
        except OSError as exc:
            raise ProviderUnavailableError(f"Ollama not reachable at {self._host}") from exc
        except HTTPException as exc:
            raise ProviderUnavailableError(
                f"connection lost before the full response from {self._host}{path}"
            ) from exc


class OllamaGenerationProvider:
    def __init__(self, host: str, model: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.model = model
        self._http = _OllamaHttp(host, timeout)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        payload: dict = {
            "model": self.model,
            "prompt": request.prompt,
            "stream": False,
            # Reasoning models (qwen3) think by default; on CPU that burns
            # minutes before the first visible token. Ignored by plain models.
            "think": False,
            "options": {"temperature": request.temperature},
        }
        if request.system:
            payload["system"] = request.system
        data = self._http.post("/api/generate", payload, self.model)
        text = data.get("response") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise InvalidProviderResponseError(
                "generate response is missing a string 'response' field"
            )
        return GenerationResult(text=text, model=self.model)

    def stream_generate(self, request: GenerationRequest) -> Iterator[str]:
        """Token stream over Ollama's NDJSON protocol (``stream: true``)."""
        payload: dict = {
            "model": self.model,
            "prompt": request.prompt,
            "stream": True,
            "think": False,
            "options": {"temperature": request.temperature},
        }
        if request.system:
            payload["system"] = request.system
        for data in self._http.stream("/api/generate", payload, self.model):
            if not isinstance(data, dict):
                raise InvalidProviderResponseError("stream line is not a JSON object")
            if data.get("done"):
                return
            chunk = data.get("response")
            if not isinstance(chunk, str):
                raise InvalidProviderResponseError(
                    "stream line is missing a string 'response' field"
                )
            if chunk:
                yield chunk


class OllamaEmbeddingProvider:
    def __init__(
        self,
        host: str,
        model: str,
        expected_dimensions: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self._expected_dimensions = expected_dimensions
        self._http = _OllamaHttp(host, timeout)

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model)
        data = self._http.post("/api/embed", {"model": self.model, "input": list(texts)}, self.model)
        vectors = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(vectors, list) or not all(isinstance(v, list) for v in vectors):
            raise InvalidProviderResponseError("'embeddings' is not a list of vectors")
        if len(vectors) != len(texts):
            raise InvalidProviderResponseError(
                f"got {len(vectors)} vectors for {len(texts)} inputs"
            )
        dimensions = {len(vector) for vector in vectors}
        if dimensions != {self._expected_dimensions}:
            raise EmbeddingDimensionError(
                f"expected {self._expected_dimensions} dimensions, got {sorted(dimensions)}"
            )
        return EmbeddingResult(vectors=vectors, model=self.model)
