"""Task 6c: setup dependency checks and the resumable gate."""

import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from foundation.config import Settings
from identity_access.setup import (
    DependencyCheck,
    SetupDependenciesFailed,
    SetupReadiness,
    _probe_database,
    _probe_ollama,
    _probe_redis,
    _probe_storage,
    check_setup_dependencies,
    initialize_workspace,
)


@pytest.fixture()
def settings() -> Settings:
    return Settings.load()


def _readiness(ok: bool) -> SetupReadiness:
    return SetupReadiness(
        checks=[DependencyCheck("database", ok, "probe", "fix it")]
    )


def test_all_passing_checks_report_ok(settings: Settings) -> None:
    async def probe(s: Settings) -> tuple[bool, str]:
        return True, "fine"

    readiness = asyncio.run(check_setup_dependencies(settings, probe_database=probe))
    assert readiness.ok is True
    assert readiness.failures() == []


def test_failing_check_is_listed_with_remediation(settings: Settings) -> None:
    async def probe(s: Settings) -> tuple[bool, str]:
        return False, "ConnectionRefusedError"

    readiness = asyncio.run(check_setup_dependencies(settings, probe_database=probe))
    assert readiness.ok is False
    assert readiness.failures() == [
        DependencyCheck(
            "database",
            False,
            "ConnectionRefusedError",
            "run: make up (starts PostgreSQL)",
        )
    ]


def test_initialize_workspace_refuses_failed_dependencies(settings: Settings) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("owner creation must not run when dependencies fail")

    with pytest.raises(SetupDependenciesFailed) as excinfo:
        asyncio.run(
            initialize_workspace(
                boom,  # type: ignore[arg-type]
                boom,  # type: ignore[arg-type]
                settings,
                "owner@example.com",
                "password-1",
                readiness=_readiness(False),
            )
        )
    assert excinfo.value.readiness.failures()[0].remediation == "fix it"


def test_probe_database_reports_unreachable(settings: Settings) -> None:
    isolated = replace(settings, database_url="postgresql+asyncpg://x:x@127.0.0.1:1/x")
    ok, detail = asyncio.run(_probe_database(isolated))
    assert ok is False
    assert detail


def test_probe_redis_reports_unreachable(settings: Settings) -> None:
    isolated = replace(settings, redis_url="redis://127.0.0.1:1/0")
    ok, _detail = asyncio.run(_probe_redis(isolated))
    assert ok is False


def test_probe_storage_writable_and_unwritable(tmp_path: Path, settings: Settings) -> None:
    ok, detail = asyncio.run(_probe_storage(replace(settings, data_dir=str(tmp_path))))
    assert (ok, detail) == (True, "writable")

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupies the path")
    ok, detail = asyncio.run(
        _probe_storage(replace(settings, data_dir=str(blocker)))
    )
    assert ok is False
    assert "unwritable" in detail


class _OllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"models": [{"name": "qwen3:4b"}, {"name": "bge-m3"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def fake_ollama() -> str:
    server = HTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_probe_ollama_missing_model_fails(settings: Settings, fake_ollama: str) -> None:
    isolated = replace(settings, ollama_host=fake_ollama)  # profile wants qwen3:8b
    ok, detail = asyncio.run(_probe_ollama(isolated))
    assert ok is False
    assert "qwen3:8b" in detail


def test_probe_ollama_available_model_passes(settings: Settings, fake_ollama: str) -> None:
    isolated = replace(settings, ollama_host=fake_ollama, profile="compact")
    ok, detail = asyncio.run(_probe_ollama(isolated))
    assert (ok, detail) == (True, "qwen3:4b + bge-m3 available")


def test_probe_ollama_unreachable_host_fails(settings: Settings) -> None:
    isolated = replace(settings, ollama_host="http://127.0.0.1:1")
    ok, _detail = asyncio.run(_probe_ollama(isolated))
    assert ok is False
