"""GLM general-API adapters for generation and query rewriting (Task 23a).

The GLM chat-completions endpoint is OpenAI-shaped; the base URL is
owner-configured (mainland vs international endpoint is the spec §15
decision, still Open). Payloads carry exactly the consented scope (spec
§16): generation sends the answering pipeline's policy/question/evidence
prompt, rewriting sends only its policy, the question, and at most the four
latest visible history messages.

The API key is a secret *reference*: it arrives through the
``SMART_RAG_GLM_API_KEY`` environment variable (explicit ``api_key=``
injection exists for tests) and is read at call time, so rotation needs no
restart. It appears only in the Authorization header — never in payloads,
error messages, or logs. Failures map to the Task 10 typed errors and
propagate; no code path falls back to another provider.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import HTTPException
from typing import Protocol

from ai_providers.contracts import (
    GenerationRequest,
    GenerationResult,
    InvalidProviderResponseError,
    ModelMissingError,
    ProviderError,
    ProviderUnavailableError,
)
from retrieval_answering.conversation import (
    HISTORY_MESSAGE_LIMIT,
    ConversationContext,
)

GLM_API_KEY_ENV = "SMART_RAG_GLM_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 60.0

REWRITE_POLICY = (
    "You rewrite a follow-up question into one standalone search query for a "
    "company knowledge base.\n"
    "Rules:\n"
    "- Resolve pronouns and ellipses using the conversation history.\n"
    "- Keep the question's language.\n"
    "- Return only the rewritten question, with no commentary."
)
GLM_REWRITE_POLICY = REWRITE_POLICY


class MissingApiKeyError(ProviderError):
    """The secret reference is unset in the environment."""

    reason = "missing_key"


class InvalidApiKeyError(ProviderError):
    """The host rejected the configured key (HTTP 401/403)."""

    reason = "invalid_key"


class _Transport(Protocol):
    def __call__(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int, bytes]: ...  # pragma: no branch


def _urllib_transport(
    url: str, payload: dict, headers: dict, timeout: float
) -> tuple[int, bytes]:
    """Production transport; HTTP errors return (status, body) for mapping."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # Body is deliberately dropped here too: provider error bodies may
        # echo request data (the invalid-key fixture proves the mapping
        # stays leak-free even when they do).
        return exc.code, b""
    except TimeoutError as exc:
        raise ProviderUnavailableError(
            f"GLM timed out after {timeout:g}s at {url}", reason="timeout"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderUnavailableError(f"GLM not reachable at {url}") from exc
    except HTTPException as exc:
        raise ProviderUnavailableError(
            f"connection lost before the full response from {url}"
        ) from exc


@dataclass(frozen=True)
class GLMConfig:
    base_url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS


def _complete(
    config: GLMConfig,
    messages: list[dict],
    temperature: float,
    *,
    transport: _Transport,
    api_key: str | None,
) -> str:
    key = api_key or os.environ.get(GLM_API_KEY_ENV)
    if not key:
        raise MissingApiKeyError(
            f"set {GLM_API_KEY_ENV} to use the GLM generation provider"
        )
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    status, body = transport(url, payload, headers, config.timeout)
    if status in (401, 403):
        raise InvalidApiKeyError(f"{url} rejected the API key (HTTP {status})")
    if status == 404:
        raise ModelMissingError(f"model {config.model!r} not found at {url} (HTTP 404)")
    if status != 200:
        raise ProviderUnavailableError(f"{url} returned HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise InvalidProviderResponseError(f"non-JSON response from {url}") from exc
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidProviderResponseError(
            f"chat completion is missing choices[0].message.content from {url}"
        ) from exc
    if not isinstance(text, str):
        raise InvalidProviderResponseError(
            f"chat completion content is not a string from {url}"
        )
    return text


def _history_messages(history: ConversationContext) -> list[dict]:
    return [
        {"role": message.role, "content": message.content}
        for message in history.messages[-HISTORY_MESSAGE_LIMIT:]
    ]


class GLMGenerationProvider:
    def __init__(
        self,
        config: GLMConfig,
        *,
        transport: _Transport | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = config.model
        self._config = config
        self._transport = transport or _urllib_transport
        self._api_key = api_key

    def generate(self, request: GenerationRequest) -> GenerationResult:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        text = _complete(
            self._config,
            messages,
            request.temperature,
            transport=self._transport,
            api_key=self._api_key,
        )
        return GenerationResult(text=text, model=self.model)


class GLMQueryRewriter:
    """Sends only the rewrite policy, the question, and the history window."""

    def __init__(
        self,
        config: GLMConfig,
        *,
        transport: _Transport | None = None,
        api_key: str | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or _urllib_transport
        self._api_key = api_key

    def rewrite(self, question: str, history: ConversationContext) -> str:
        messages = [
            {"role": "system", "content": REWRITE_POLICY},
            *_history_messages(history),
        ]
        messages.append({"role": "user", "content": question})
        return _complete(
            self._config,
            messages,
            0.0,
            transport=self._transport,
            api_key=self._api_key,
        )


# ponytail: generation is non-streaming (stream_answer degrades to one delta
# frame); add SSE stream_generate when token-by-token rendering matters.
