"""Task 4: Compose topology health — liveness, readiness, logs, shutdown, worker.

The liveness/readiness endpoint tests run against the live stack (`make up`)
and skip when it is not running, so `make check` stays green on machines
without Docker. The checker, logging, and worker composition-root tests are
self-contained and always run.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get("SMART_RAG_TEST_BASE_URL", "http://127.0.0.1:8000")


def _api_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health/live", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


requires_api = pytest.mark.skipif(not _api_up(), reason="API not running (make up first)")


# --- live endpoints ----------------------------------------------------------


@requires_api
def test_liveness_reports_process_health() -> None:
    with urllib.request.urlopen(f"{BASE_URL}/health/live", timeout=5) as response:
        assert response.status == 200
        assert json.load(response)["status"] == "live"


@requires_api
def test_readiness_reports_healthy_dependencies() -> None:
    with urllib.request.urlopen(f"{BASE_URL}/health/ready", timeout=10) as response:
        assert response.status == 200
        body = json.load(response)
    assert body["status"] == "ready"
    assert body["checks"]["postgres"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is True


# --- readiness fail-closed behaviour (no live stack needed) -------------------


def test_readiness_fails_closed_when_dependencies_unreachable() -> None:
    from apps.api.main import ReadinessChecker

    checker = ReadinessChecker(
        database_url="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
    )
    status, body = asyncio.run(checker.run())

    assert status == 503
    assert body["status"] == "unavailable"
    assert body["checks"]["postgres"]["ok"] is False
    assert body["checks"]["redis"]["ok"] is False
    # Failures must stay actionable but never leak connection strings.
    assert "127.0.0.1" not in json.dumps(body)


# --- structured logging ------------------------------------------------------


def test_structured_logs_redact_secrets_and_carry_correlation_id() -> None:
    import logging

    from apps.api.main import JsonFormatter, correlation_id

    formatter = JsonFormatter(secrets=("hunter2",))
    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname=__file__, lineno=1,
        msg="connecting with password hunter2", args=(), exc_info=None,
    )
    token = correlation_id.set("c-123")
    try:
        line = formatter.format(record)
    finally:
        correlation_id.reset(token)

    payload = json.loads(line)
    assert "hunter2" not in line
    assert "[redacted]" in payload["message"]
    assert payload["correlation_id"] == "c-123"
    assert payload["level"] == "INFO"


# --- graceful shutdown -------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal test")
def test_api_shuts_down_gracefully_on_sigterm() -> None:
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": "src"}
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for_live(port, timeout=30), "uvicorn did not become live"
        process.send_signal(signal.SIGTERM)
        # uvicorn drains in-flight work and exits; >=0.34 re-raises SIGTERM
        # after a clean shutdown, so 0 and -15/143 are both graceful exits.
        assert process.wait(timeout=15) in (0, -15), f"uvicorn exited with {process.returncode}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_live(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.3)
    return False


# --- worker composition root -------------------------------------------------


def test_worker_exposes_redis_broker_and_ping_actor() -> None:
    import dramatiq

    import apps.worker.main as worker

    assert worker.broker is dramatiq.get_broker()
    assert worker.broker.get_actor("ping").fn() == "pong"


def test_worker_reimport_keeps_redis_broker() -> None:
    import importlib

    import dramatiq
    from dramatiq.brokers.redis import RedisBroker
    from dramatiq.brokers.stub import StubBroker

    import apps.worker.main as worker

    dramatiq.set_broker(StubBroker())
    reloaded = importlib.reload(worker)

    assert isinstance(reloaded.broker, RedisBroker)
