"""Task 23a: hosted GLM payloads carry exactly the consented scope.

Spec §16: hosted generation sends the answer policy, current question,
selected passage text, citation labels/IDs, and — if enabled — up to four
visible conversation messages. Rewriting sends only its rewrite policy, the
current question, and the same history limit. The API key exists only in the
Authorization header and the ``SMART_RAG_GLM_API_KEY`` reference — never in a
payload, an error message, or a log line. Failures surface as typed errors;
nothing falls back to another provider.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ai_providers.contracts import (
    GenerationRequest,
    InvalidProviderResponseError,
    ModelMissingError,
    ProviderUnavailableError,
)
from ai_providers.glm import (
    GLM_API_KEY_ENV,
    GLM_REWRITE_POLICY,
    GLMConfig,
    GLMGenerationProvider,
    GLMQueryRewriter,
    InvalidApiKeyError,
    MissingApiKeyError,
)
from retrieval_answering.answering import stream_answer
from retrieval_answering.conversation import ConversationContext
from retrieval_answering.retrieval import Evidence, RankedEvidence

API_KEY = "sk-secret-glm-key-123"
QUESTION = "What is the allowance?"
EVIDENCE_TEXT = "The allowance is 500 per month."


class FakeTransport:
    """Captures requests and returns a canned (status, body)."""

    def __init__(
        self, status: int = 200, body: object | None = None, raw: bytes | None = None
    ):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, payload: dict, headers: dict, timeout: float):
        self.calls.append((url, payload, headers))
        return self.status, self.raw


def chat_body(content: str = "500 per month [1:quote].") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def glm_config() -> GLMConfig:
    return GLMConfig(base_url="https://glm.example/api/paas/v4", model="glm-4-flash")


def generation(transport: FakeTransport, **kwargs) -> GLMGenerationProvider:
    return GLMGenerationProvider(
        glm_config(), transport=transport, api_key=API_KEY, **kwargs
    )


def rewriter(transport: FakeTransport, **kwargs) -> GLMQueryRewriter:
    return GLMQueryRewriter(
        glm_config(), transport=transport, api_key=API_KEY, **kwargs
    )


# --- generation payload -----------------------------------------------------


def test_generation_payload_carries_exactly_the_consented_scope() -> None:
    transport = FakeTransport(body=chat_body())
    request = GenerationRequest(
        prompt=f"Evidence:\n[1] (source: handbook, page 2)\n{EVIDENCE_TEXT}"
        f'\n\nQuestion: {QUESTION}\n\nAnswer with [1:"{EVIDENCE_TEXT}"] markers.',
        system="Answer policy text.",
    )
    result = generation(transport).generate(request)

    assert result.model == "glm-4-flash"
    assert result.text == chat_body()["choices"][0]["message"]["content"]
    url, payload, headers = transport.calls[0]
    assert url == "https://glm.example/api/paas/v4/chat/completions"
    assert set(payload) == {"model", "messages", "temperature", "stream"}
    assert payload["model"] == "glm-4-flash"
    assert payload["stream"] is False
    assert payload["temperature"] == request.temperature
    assert payload["messages"] == [
        {"role": "system", "content": request.system},
        {"role": "user", "content": request.prompt},
    ]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    # The key travels in the header only, never in the payload body.
    assert API_KEY not in json.dumps(payload)


def test_generation_without_system_omits_the_field_value() -> None:
    transport = FakeTransport(body=chat_body())
    generation(transport).generate(GenerationRequest(prompt=QUESTION))
    _, payload, _ = transport.calls[0]
    assert payload["messages"] == [{"role": "user", "content": QUESTION}]


def test_api_key_is_read_from_the_env_reference() -> None:
    transport = FakeTransport(body=chat_body())
    provider = GLMGenerationProvider(glm_config(), transport=transport)
    monkey_env = pytest.MonkeyPatch()
    monkey_env.setenv(GLM_API_KEY_ENV, API_KEY)
    try:
        provider.generate(GenerationRequest(prompt=QUESTION))
    finally:
        monkey_env.undo()
    assert transport.calls[0][2]["Authorization"] == f"Bearer {API_KEY}"


def test_missing_env_key_is_typed_and_actionable() -> None:
    transport = FakeTransport(body=chat_body())
    provider = GLMGenerationProvider(glm_config(), transport=transport)
    monkey_env = pytest.MonkeyPatch()
    monkey_env.delenv(GLM_API_KEY_ENV, raising=False)
    try:
        with pytest.raises(MissingApiKeyError) as failure:
            provider.generate(GenerationRequest(prompt=QUESTION))
    finally:
        monkey_env.undo()
    assert failure.value.reason == "missing_key"
    assert GLM_API_KEY_ENV in str(failure.value)
    assert transport.calls == []


# --- failure mapping: typed, leak-free, no fallback --------------------------


def test_invalid_key_is_typed_and_leak_free() -> None:
    body = {"error": {"code": "401", "message": f"bad key {API_KEY}"}}
    transport = FakeTransport(status=401, body=body)
    with pytest.raises(InvalidApiKeyError) as failure:
        generation(transport).generate(GenerationRequest(prompt=QUESTION))
    assert failure.value.reason == "invalid_key"
    assert API_KEY not in str(failure.value)
    # The provider's error body (which echoes the key) is dropped entirely.
    assert "bad key" not in str(failure.value)


def test_missing_model_is_typed() -> None:
    transport = FakeTransport(
        status=404, body={"error": {"message": "model not found"}}
    )
    with pytest.raises(ModelMissingError):
        generation(transport).generate(GenerationRequest(prompt=QUESTION))


def test_outage_http_503_is_unavailable() -> None:
    transport = FakeTransport(status=503, body={"error": {"message": "overloaded"}})
    with pytest.raises(ProviderUnavailableError):
        generation(transport).generate(GenerationRequest(prompt=QUESTION))


def test_transport_failure_propagates_without_fallback() -> None:
    def down(url: str, payload: dict, headers: dict, timeout: float):
        raise ProviderUnavailableError("glm.example is down")

    provider = GLMGenerationProvider(glm_config(), transport=down, api_key=API_KEY)
    with pytest.raises(ProviderUnavailableError):
        provider.generate(GenerationRequest(prompt=QUESTION))


def test_non_json_body_is_invalid() -> None:
    transport = FakeTransport(raw=b"<html>gateway error</html>")
    with pytest.raises(InvalidProviderResponseError):
        generation(transport).generate(GenerationRequest(prompt=QUESTION))


def test_response_without_choices_is_invalid() -> None:
    transport = FakeTransport(body={"id": "x"})
    with pytest.raises(InvalidProviderResponseError):
        generation(transport).generate(GenerationRequest(prompt=QUESTION))


def test_stream_answer_surfaces_hosted_error_without_local_answer() -> None:
    class DownGLM(GLMGenerationProvider):
        def generate(self, request: GenerationRequest):
            raise InvalidApiKeyError("glm.example rejected the API key")

    ranked = RankedEvidence(
        query_language="en",
        items=[
            Evidence(
                chunk_id="chunk-1",
                source_id="src-1",
                source_name="handbook",
                text=EVIDENCE_TEXT,
                language="en",
                page=1,
                block_start=0,
                block_end=1,
                rank=1,
                semantic_rank=1,
                keyword_rank=None,
                score=0.03,
            )
        ],
    )
    frames = asyncio.run(
        _collect(
            stream_answer(
                QUESTION, ranked, generation=DownGLM(glm_config()), request_id="r1"
            )
        )
    )
    kinds = [frame.kind for frame in frames]
    assert "delta" not in kinds
    error = next(frame for frame in frames if frame.kind == "error")
    assert error.reason == "invalid_key"


async def _collect(events):
    return [event async for event in events]


# --- rewriting payload -------------------------------------------------------


def history(*lines: str) -> ConversationContext:
    context = ConversationContext()
    for index, line in enumerate(lines, 1):
        context = context.appended("user" if index % 2 else "assistant", line)
    return context


def test_rewrite_sends_only_policy_question_and_history() -> None:
    transport = FakeTransport(body=chat_body("allowance question"))
    question = rewriter(transport).rewrite(
        "what is the allowance?", history("earlier question", "earlier answer")
    )
    assert question == "allowance question"
    url, payload, _ = transport.calls[0]
    assert url == "https://glm.example/api/paas/v4/chat/completions"
    assert payload["messages"] == [
        {"role": "system", "content": GLM_REWRITE_POLICY},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "what is the allowance?"},
    ]
    # Source text from the knowledge base never rides along with a rewrite.
    assert EVIDENCE_TEXT not in json.dumps(payload)


def test_rewrite_history_is_capped_at_the_four_latest_messages() -> None:
    transport = FakeTransport(body=chat_body("q"))
    rewriter(transport).rewrite("q?", history(*[f"m{i}" for i in range(1, 7)]))
    _, payload, _ = transport.calls[0]
    history_messages = payload["messages"][1:-1]
    assert len(history_messages) == 4
    assert [message["content"] for message in history_messages] == [
        "m3",
        "m4",
        "m5",
        "m6",
    ]


def test_rewrite_without_history_sends_policy_and_question_only() -> None:
    transport = FakeTransport(body=chat_body("q"))
    rewriter(transport).rewrite("q?", ConversationContext())
    _, payload, _ = transport.calls[0]
    assert payload["messages"] == [
        {"role": "system", "content": GLM_REWRITE_POLICY},
        {"role": "user", "content": "q?"},
    ]


# --- production urllib transport (scripted local server) ---------------------


class _StubGLMServer:
    """Local server so requests run through the real urllib transport."""

    def __init__(self, mode: str = "ok", status: int = 200) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if mode == "slow":
                    time.sleep(0.5)
                    return
                if mode == "truncate":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                data = json.dumps(chat_body()).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _live_provider(
    server: _StubGLMServer, timeout: float = 5.0
) -> GLMGenerationProvider:
    return GLMGenerationProvider(
        GLMConfig(base_url=server.base_url, model="glm-4-flash", timeout=timeout),
        api_key=API_KEY,
    )


def test_production_transport_returns_typed_result() -> None:
    server = _StubGLMServer()
    try:
        result = _live_provider(server).generate(GenerationRequest(prompt=QUESTION))
    finally:
        server.stop()
    assert result.model == "glm-4-flash"
    assert result.text


def test_production_transport_maps_http_status() -> None:
    server = _StubGLMServer(status=401)
    try:
        provider = _live_provider(server)
        with pytest.raises(InvalidApiKeyError):
            provider.generate(GenerationRequest(prompt=QUESTION))
    finally:
        server.stop()


def test_production_transport_maps_read_timeout() -> None:
    server = _StubGLMServer(mode="slow")
    try:
        provider = _live_provider(server, timeout=0.2)
        with pytest.raises(ProviderUnavailableError) as failure:
            provider.generate(GenerationRequest(prompt=QUESTION))
    finally:
        server.stop()
    assert failure.value.reason == "timeout"


def test_production_transport_survives_truncated_body() -> None:
    server = _StubGLMServer(mode="truncate")
    try:
        provider = _live_provider(server)
        with pytest.raises(ProviderUnavailableError):
            provider.generate(GenerationRequest(prompt=QUESTION))
    finally:
        server.stop()


def test_refused_connection_is_unreachable() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    provider = GLMGenerationProvider(
        GLMConfig(base_url=f"http://127.0.0.1:{port}", model="glm-4-flash"),
        api_key=API_KEY,
    )
    with pytest.raises(ProviderUnavailableError):
        provider.generate(GenerationRequest(prompt=QUESTION))


def test_non_string_content_is_invalid() -> None:
    transport = FakeTransport(body={"choices": [{"message": {"content": 123}}]})
    with pytest.raises(InvalidProviderResponseError):
        generation(transport).generate(GenerationRequest(prompt=QUESTION))
