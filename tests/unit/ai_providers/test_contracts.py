"""Task 10: provider contracts — fakes, Ollama adapters, typed errors.

The Ollama adapter runs against a scripted local HTTP server, so the blocking
suite stays deterministic with no network. A separate live smoke test covers
the real Ollama host when it is running.
"""

import json
import socket
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ai_providers.contracts import (
    EmbeddingDimensionError,
    EmbeddingResult,
    GenerationRequest,
    GenerationResult,
    InvalidProviderResponseError,
    ModelMissingError,
    ProviderUnavailableError,
)
from ai_providers.fakes import FakeEmbeddingProvider, FakeGenerationProvider
from ai_providers.ollama import (
    OllamaEmbeddingProvider,
    OllamaGenerationProvider,
)

# --- deterministic fakes -------------------------------------------------


def test_fake_generation_returns_scripted_result_and_records_request() -> None:
    scripted = GenerationResult(text="scripted", model="fake-chat")
    provider = FakeGenerationProvider(script=[scripted])

    result = provider.generate(GenerationRequest(prompt="hello"))

    assert result is scripted
    assert [r.prompt for r in provider.requests] == ["hello"]


def test_fake_generation_falls_back_to_default_text() -> None:
    provider = FakeGenerationProvider(default_text="fallback answer")

    result = provider.generate(GenerationRequest(prompt="anything", system="sys"))

    assert result.text == "fallback answer"
    assert provider.requests[0].system == "sys"


def test_fake_generation_raises_scripted_error_once() -> None:
    provider = FakeGenerationProvider(script=[ProviderUnavailableError("down")])

    with pytest.raises(ProviderUnavailableError):
        provider.generate(GenerationRequest(prompt="hello"))
    assert provider.generate(GenerationRequest(prompt="hello")).text == "fake answer"


def test_fake_embeddings_are_deterministic_and_dimensional() -> None:
    provider = FakeEmbeddingProvider(dimension=4)

    first = provider.embed(["hello", "world"])
    second = provider.embed(["hello"])

    assert isinstance(first, EmbeddingResult)
    assert first.count == 2
    assert all(len(vector) == 4 for vector in first.vectors)
    assert first.vectors[0] == second.vectors[0]
    assert [r for r in provider.requests] == [["hello", "world"], ["hello"]]


def test_fake_embeddings_raise_scripted_error() -> None:
    provider = FakeEmbeddingProvider(dimension=4, script=[ModelMissingError("gone")])

    with pytest.raises(ModelMissingError):
        provider.embed(["hello"])


def test_fake_embeddings_return_scripted_result() -> None:
    scripted = EmbeddingResult(vectors=[[1.0, 0.0, 0.0, 0.0]], model="fake-embed")
    provider = FakeEmbeddingProvider(dimension=4, script=[scripted])

    assert provider.embed(["hello"]) is scripted


# --- Ollama adapters (scripted local HTTP server) ------------------------


class _StubServer:
    def __init__(self, handler_fn) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append(json.loads(body) if body else {})
                status, payload = handler_fn(outer.requests[-1])
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self.requests: list[dict] = []
        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def stub_ollama():
    servers = []

    def factory(handler_fn) -> _StubServer:
        server = _StubServer(handler_fn)
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()


def _generation_provider(host: str, timeout: float = 30) -> OllamaGenerationProvider:
    return OllamaGenerationProvider(host, model="qwen3:8b", timeout=timeout)


def _embedding_provider(host: str, timeout: float = 30) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(host, model="bge-m3", expected_dimensions=4, timeout=timeout)


def test_generation_disables_thinking_for_reasoning_models(stub_ollama) -> None:
    # qwen3 thinks by default; on CPU that burns minutes before the first
    # visible token. The adapter opts out explicitly.
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"response": "ok"}

    server = stub_ollama(handler)
    _generation_provider(server.host).generate(GenerationRequest(prompt="hi"))

    assert server.requests[0]["think"] is False


def test_generation_returns_text_and_keeps_streaming_off(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"response": "grounded answer"}

    server = stub_ollama(handler)
    result = _generation_provider(server.host).generate(GenerationRequest(prompt="q", system="s"))

    assert result == GenerationResult(text="grounded answer", model="qwen3:8b")
    assert server.requests[0]["stream"] is False
    assert server.requests[0]["model"] == "qwen3:8b"
    assert server.requests[0]["prompt"] == "q"
    assert server.requests[0]["system"] == "s"


def test_generation_without_system_omits_field(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"response": "ok"}

    server = stub_ollama(handler)
    _generation_provider(server.host).generate(GenerationRequest(prompt="q"))

    assert "system" not in server.requests[0]


def test_missing_model_is_typed(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 404, {"error": "model 'qwen3:8b' not found, try pulling it first"}

    with pytest.raises(ModelMissingError) as excinfo:
        _generation_provider(stub_ollama(handler).host).generate(GenerationRequest(prompt="SECRET-PROMPT"))

    assert excinfo.value.reason == "missing_model"
    assert "SECRET-PROMPT" not in str(excinfo.value)


def test_http_error_never_leaks_response_body(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict | bytes]:
        return 500, b"SECRET-SERVER-DETAIL"

    with pytest.raises(ProviderUnavailableError) as excinfo:
        _generation_provider(stub_ollama(handler).host).generate(GenerationRequest(prompt="SECRET-PROMPT"))

    assert excinfo.value.reason == "unreachable"
    assert "SECRET-SERVER-DETAIL" not in str(excinfo.value)
    assert "SECRET-PROMPT" not in str(excinfo.value)


def test_connection_refused_is_unreachable() -> None:
    # Port 1 on loopback is closed; nothing listens there.
    provider = _generation_provider("http://127.0.0.1:1", timeout=2)

    with pytest.raises(ProviderUnavailableError) as excinfo:
        provider.generate(GenerationRequest(prompt="q"))

    assert excinfo.value.reason == "unreachable"


def test_slow_host_times_out(stub_ollama) -> None:
    import time

    def handler(request: dict) -> tuple[int, dict]:
        time.sleep(1.5)
        return 200, {"response": "too late"}

    with pytest.raises(ProviderUnavailableError) as excinfo:
        _generation_provider(stub_ollama(handler).host, timeout=0.25).generate(
            GenerationRequest(prompt="q")
        )

    assert excinfo.value.reason == "timeout"


@contextmanager
def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    class Quiet(HTTPServer):
        def handle_error(self, request: object, client_address: object) -> None:
            pass  # handler-side errors (e.g. flushing to a reset socket) are expected here

    server = Quiet(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_body_read_timeout_is_typed_timeout() -> None:
    class StallHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b'{"resp')
            self.wfile.flush()
            time.sleep(1.5)  # client (timeout=0.25s) times out on the stalled body
            self.wfile.write(b'onment"}' + b" " * 54)

        def log_message(self, *args: object) -> None:
            pass

    with _serve(StallHandler) as host:
        provider = OllamaGenerationProvider(host, model="qwen3:8b", timeout=0.25)
        with pytest.raises(ProviderUnavailableError) as excinfo:
            provider.generate(GenerationRequest(prompt="q"))

    assert excinfo.value.reason == "timeout"


def test_connection_reset_mid_body_is_unreachable() -> None:
    class ResetHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b'{"resp')
            self.wfile.flush()
            # Hard-reset (RST, not EOF) so the client read fails mid-body.
            self.connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            self.connection.close()

        def log_message(self, *args: object) -> None:
            pass

    with _serve(ResetHandler) as host, pytest.raises(ProviderUnavailableError) as excinfo:
        OllamaGenerationProvider(host, model="qwen3:8b").generate(GenerationRequest(prompt="q"))

    assert excinfo.value.reason == "unreachable"


def test_truncated_body_is_unreachable() -> None:
    class TruncatingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b'{"resp')  # promise 64 bytes, send 6, then close cleanly

        def log_message(self, *args: object) -> None:
            pass

    with _serve(TruncatingHandler) as host, pytest.raises(ProviderUnavailableError) as excinfo:
        OllamaGenerationProvider(host, model="qwen3:8b").generate(GenerationRequest(prompt="q"))

    assert excinfo.value.reason == "unreachable"


def test_non_json_response_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict | bytes]:
        return 200, b"<html>not-json"

    with pytest.raises(InvalidProviderResponseError) as excinfo:
        _generation_provider(stub_ollama(handler).host).generate(GenerationRequest(prompt="q"))

    assert excinfo.value.reason == "invalid_response"


def test_generation_missing_text_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"unexpected": "shape"}

    with pytest.raises(InvalidProviderResponseError):
        _generation_provider(stub_ollama(handler).host).generate(GenerationRequest(prompt="q"))


def test_generation_non_string_text_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"response": 42}

    with pytest.raises(InvalidProviderResponseError):
        _generation_provider(stub_ollama(handler).host).generate(GenerationRequest(prompt="q"))


def test_embeddings_return_typed_result(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"model": "bge-m3", "embeddings": [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]}

    server = stub_ollama(handler)
    result = _embedding_provider(server.host).embed(["one", "two"])

    assert result == EmbeddingResult(vectors=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]], model="bge-m3")
    assert result.dimensions == 4
    assert server.requests[0]["input"] == ["one", "two"]


def test_empty_embed_skips_http_call() -> None:
    # Host points at a closed port; an empty batch must not touch the network.
    result = _embedding_provider("http://127.0.0.1:1").embed([])

    assert result.count == 0
    assert result.model == "bge-m3"


def test_embeddings_wrong_count_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"embeddings": [[0.1, 0.2, 0.3, 0.4]]}

    with pytest.raises(InvalidProviderResponseError):
        _embedding_provider(stub_ollama(handler).host).embed(["one", "two"])


def test_embeddings_dimension_mismatch_is_typed(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"embeddings": [[0.1, 0.2, 0.3]]}

    with pytest.raises(EmbeddingDimensionError) as excinfo:
        _embedding_provider(stub_ollama(handler).host).embed(["one"])

    assert excinfo.value.reason == "dimension_mismatch"


def test_embeddings_ragged_vectors_are_dimension_mismatch(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"embeddings": [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7]]}

    with pytest.raises(EmbeddingDimensionError):
        _embedding_provider(stub_ollama(handler).host).embed(["one", "two"])


def test_embeddings_missing_payload_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, dict]:
        return 200, {"nope": True}

    with pytest.raises(InvalidProviderResponseError):
        _embedding_provider(stub_ollama(handler).host).embed(["one"])


# --- streaming generation (Task 14b) --------------------------------------


def _ndjson(lines: list[dict]) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def test_stream_generate_yields_chunks_skips_empty_and_stops_at_done(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, bytes]:
        return 200, _ndjson([
            {"response": "He", "done": False},
            {"response": "", "done": False},
            {"response": "llo", "done": False},
            {"response": "", "done": True},
        ])

    server = stub_ollama(handler)
    chunks = list(_generation_provider(server.host).stream_generate(
        GenerationRequest(prompt="hi", system="be brief")  # system must reach the payload
    ))

    assert chunks == ["He", "llo"]
    assert server.requests[0]["stream"] is True
    assert server.requests[0]["system"] == "be brief"


def test_stream_skips_blank_lines_and_ends_cleanly_without_done(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, bytes]:
        return 200, _ndjson([{"response": "a", "done": False}]) + b"\n" + _ndjson([
            {"response": "b", "done": False},
        ])

    provider = _generation_provider(stub_ollama(handler).host)
    lines = list(provider._http.stream("/api/generate", {"prompt": "hi"}, "qwen3:8b"))

    assert [line["response"] for line in lines] == ["a", "b"]


def test_stream_read_timeout_is_typed_timeout() -> None:
    class StallHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b'{"resp')
            self.wfile.flush()
            time.sleep(1.5)  # client (timeout=0.25s) stalls mid-body
            self.wfile.write(b'onse": "x"}' + b" " * 50)

        def log_message(self, *args: object) -> None:
            pass

    with _serve(StallHandler) as host:
        provider = OllamaGenerationProvider(host, model="qwen3:8b", timeout=0.25)
        with pytest.raises(ProviderUnavailableError) as excinfo:
            list(provider.stream_generate(GenerationRequest(prompt="q")))

    assert excinfo.value.reason == "timeout"


def test_stream_connection_reset_mid_body_is_unreachable() -> None:
    class ResetHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b'{"resp')
            self.wfile.flush()
            # Hard-reset (RST, not EOF) so the client read fails mid-body.
            self.connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            self.connection.close()

        def log_message(self, *args: object) -> None:
            pass

    with _serve(ResetHandler) as host, pytest.raises(ProviderUnavailableError) as excinfo:
        list(OllamaGenerationProvider(host, model="qwen3:8b").stream_generate(
            GenerationRequest(prompt="q")
        ))

    assert excinfo.value.reason == "unreachable"


def test_stream_truncated_chunked_body_is_unreachable() -> None:
    class ChunkedCutHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b'7\r\n{"resp')  # promise more chunks, then close cleanly

        def log_message(self, *args: object) -> None:
            pass

    with _serve(ChunkedCutHandler) as host, pytest.raises(ProviderUnavailableError):
        list(OllamaGenerationProvider(host, model="qwen3:8b").stream_generate(
            GenerationRequest(prompt="q")
        ))


def test_open_maps_connection_dropped_without_response_to_unreachable() -> None:
    class SilentHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.connection.close()  # accept, then drop without an HTTP response

        def log_message(self, *args: object) -> None:
            pass

    with _serve(SilentHandler) as host, pytest.raises(ProviderUnavailableError) as excinfo:
        OllamaGenerationProvider(host, model="qwen3:8b").generate(GenerationRequest(prompt="q"))

    assert excinfo.value.reason == "unreachable"


def test_stream_generate_non_string_chunk_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, bytes]:
        return 200, _ndjson([{"response": 5, "done": False}])

    with pytest.raises(InvalidProviderResponseError):
        list(_generation_provider(stub_ollama(handler).host).stream_generate(
            GenerationRequest(prompt="hi")
        ))


def test_stream_generate_non_dict_line_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, bytes]:
        return 200, b"[1, 2]\n"

    with pytest.raises(InvalidProviderResponseError):
        list(_generation_provider(stub_ollama(handler).host).stream_generate(
            GenerationRequest(prompt="hi")
        ))


def test_stream_generate_malformed_line_is_invalid(stub_ollama) -> None:
    def handler(request: dict) -> tuple[int, bytes]:
        return 200, b"not-json\n"

    with pytest.raises(InvalidProviderResponseError):
        list(_generation_provider(stub_ollama(handler).host).stream_generate(
            GenerationRequest(prompt="hi")
        ))


def test_stream_generate_connection_refused_is_unreachable() -> None:
    with pytest.raises(ProviderUnavailableError):
        list(_generation_provider("http://127.0.0.1:1").stream_generate(
            GenerationRequest(prompt="hi")
        ))
