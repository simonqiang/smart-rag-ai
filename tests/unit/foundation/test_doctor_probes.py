"""Coverage for doctor.py host-coupled probes and pure helpers.

The main doctor suite (test_doctor.py) injects fakes end to end; these tests
exercise the default probes and helper branches those fakes bypass.
"""

import http.server
import json
import sys
import threading

import pytest

from foundation import doctor
from foundation.doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    OllamaProbe,
    _platform_check,
    _runtime_remediation,
    _windows_build,
    default_runner,
    main,
    probe_host,
)

# --- local fakes ------------------------------------------------------------


class FakePath:
    def __init__(self, content: str):
        self._content = content

    def read_text(self, encoding: str | None = None) -> str:
        return self._content


class FakeRaisingPath:
    def read_text(self, encoding: str | None = None) -> str:
        raise OSError("no os-release")


class _TagsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"models": [{"name": "qwen3:8b"}, {"name": "bge-m3"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep test output clean
        pass


@pytest.fixture()
def tags_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _TagsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


# --- pure helpers ------------------------------------------------------------


def test_windows_build_parses_final_component() -> None:
    assert _windows_build("10.0.22631") == 22631
    assert _windows_build("garbage") == 0


def test_render_redacts_secrets_and_skips_empty_ones() -> None:
    report = DoctorReport(
        host="Test/arm64",
        results=[
            CheckResult("thing", CheckStatus.FAIL, "value=sec", remediation="do this"),
        ],
        secrets=["sec", ""],
    )
    text = report.render()
    assert "value=[redacted]" in text
    assert "fix: do this" in text


def test_runtime_remediation_is_platform_specific() -> None:
    assert "WSL2" in _runtime_remediation(_host("Windows"))
    assert "Apple Silicon" in _runtime_remediation(_host("Darwin"))
    assert "docker.io" in _runtime_remediation(_host("Linux"))


def _host(system: str):
    return type(
        "H",
        (),
        {"system": system, "machine": "x86_64", "os_id": "", "os_version": "", "version": ""},
    )()


# --- platform check fallbacks ------------------------------------------------


def test_unsupported_distribution_and_system_fail() -> None:
    fedora = _host("Linux")
    fedora.os_id, fedora.os_version = "fedora", "40"
    result = _platform_check(fedora)
    assert result.status is CheckStatus.FAIL
    assert "unsupported distribution" in result.detail

    result = _platform_check(_host("Plan9"))
    assert result.status is CheckStatus.FAIL
    assert "unsupported system" in result.detail


# --- default probes ----------------------------------------------------------


def test_default_runner_reports_command_result() -> None:
    code, out = default_runner([sys.executable, "-c", "print('probe-ok')"])
    assert code == 0
    assert "probe-ok" in out


def test_default_runner_survives_missing_command() -> None:
    code, out = default_runner(["/nonexistent/probe-binary-xyz"])
    assert code == 1
    assert out == ""


def test_probe_host_parses_os_release(monkeypatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor, "Path", lambda _p: FakePath('ID=ubuntu\nVERSION_ID="24.04"\n'))
    info = probe_host()
    assert info.system == "Linux"
    assert info.os_id == "ubuntu"
    assert info.os_version == "24.04"


def test_probe_host_tolerates_missing_os_release(monkeypatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(doctor, "Path", lambda _p: FakeRaisingPath())
    info = probe_host()
    assert info.os_id == ""
    assert info.os_version == ""


def test_ollama_probe_lists_models(tags_server: str) -> None:
    models = OllamaProbe(tags_server).list_models()
    assert models == ["qwen3:8b", "bge-m3"]


# --- entrypoint --------------------------------------------------------------


def test_main_returns_zero_and_renders_pass(monkeypatch, capsys) -> None:
    ok = DoctorReport(host="Test/arm64", results=[], secrets=[])
    monkeypatch.setattr(doctor, "run_doctor", lambda _settings: ok)
    assert main() == 0
    assert "doctor: PASS" in capsys.readouterr().out


def test_main_returns_one_and_renders_fail(monkeypatch, capsys) -> None:
    failing = DoctorReport(
        host="Test/arm64",
        results=[CheckResult("thing", CheckStatus.FAIL, "broken")],
        secrets=[],
    )
    monkeypatch.setattr(doctor, "run_doctor", lambda _settings: failing)
    assert main() == 1
    assert "doctor: FAIL" in capsys.readouterr().out
