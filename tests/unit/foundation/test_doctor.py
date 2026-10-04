"""Task 1: supported runtime profile and diagnostics.

Covers supported/rejected host checks, Docker/WSL2 availability, writable data
paths, missing Docker/Ollama/models, model-profile reporting, platform-specific
remediation text, and secret-free doctor output.
"""

from dataclasses import dataclass, field

import pytest

from foundation.config import Settings
from foundation.doctor import CheckStatus, run_doctor

# --- fakes -----------------------------------------------------------------


@dataclass(frozen=True)
class FakeHost:
    system: str  # "Windows" | "Darwin" | "Linux"
    machine: str  # "AMD64" | "ARM64" | "arm64" | "x86_64" | "aarch64"
    version: str = ""  # e.g. "10.0.22631" for Windows builds
    os_id: str = ""  # /etc/os-release ID, e.g. "ubuntu"
    os_version: str = ""


@dataclass
class FakeRunner:
    results: dict = field(default_factory=dict)  # tuple(args) -> (returncode, stdout)

    def __call__(self, args):
        return self.results.get(tuple(args), (1, ""))


class FakeOllama:
    def __init__(self, models):
        self._models = models

    def list_models(self):
        return self._models


def make_settings(tmp_path, profile="recommended", ollama_host="http://127.0.0.1:11434"):
    return Settings.load(path=None, overrides={"data_dir": str(tmp_path / "data"), "backup_dir": str(tmp_path / "backups"), "ollama_host": ollama_host, "profile": profile})


def run(
    tmp_path,
    host=FakeHost(system="Darwin", machine="arm64"),
    docker=(0, ""),
    wsl=(1, ""),
    models=("qwen3:8b", "bge-m3"),
    ollama_reachable=True,
    **kwargs,
):
    settings = make_settings(tmp_path, **kwargs)
    runner = FakeRunner(
        {
            ("docker", "info"): docker,
            ("wsl.exe", "--status"): wsl,
        }
    )
    ollama = FakeOllama(list(models)) if ollama_reachable else UnreachableOllama()
    report = run_doctor(settings, host=host, runner=runner, ollama=ollama)
    return report, settings


class UnreachableOllama:
    def list_models(self):
        raise OSError("connection refused")


def by_name(report, name):
    matches = [r for r in report.results if r.name == name]
    assert matches, f"no check named {name!r} in {[r.name for r in report.results]}"
    return matches[0]


# --- settings ---------------------------------------------------------------


def test_settings_load_pins_owner_confirmed_limits_and_profile():
    s = Settings.load()
    assert s.profile == "recommended"
    assert s.active_profile.chat_model == "qwen3:8b"
    assert s.active_profile.embedding_model == "bge-m3"
    assert s.active_profile.embedding_dimensions == 1024
    compact = s.profiles["compact"]
    assert compact.chat_model == "qwen3:4b"
    assert s.limits.upload_mb == 50
    assert s.limits.max_pages_per_document == 2000
    assert s.limits.decompressed_mb == 500
    assert s.limits.crawl.max_pages == 500
    assert s.limits.crawl.max_depth == 5
    assert s.limits.crawl.max_minutes == 30
    assert s.limits.crawl.max_response_mb == 10
    assert s.limits.retention.superseded_days == 30
    assert s.limits.retention.conversation_days == 90


def test_settings_load_rejects_unknown_profile():
    with pytest.raises(ValueError, match="profile"):
        Settings.load(overrides={"profile": "turbo"})


# --- platform checks --------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        FakeHost(system="Windows", machine="AMD64", version="10.0.22631"),
        FakeHost(system="Darwin", machine="arm64"),
        FakeHost(system="Linux", machine="x86_64", os_id="ubuntu", os_version="22.04"),
        FakeHost(system="Linux", machine="x86_64", os_id="ubuntu", os_version="24.04"),
    ],
)
def test_supported_hosts_pass(tmp_path, host):
    report, _ = run(tmp_path, host=host)
    assert by_name(report, "supported_platform").status is CheckStatus.OK


@pytest.mark.parametrize(
    "host",
    [
        (FakeHost(system="Darwin", machine="x86_64"), "Apple Silicon"),  # Intel Mac
        (FakeHost(system="Windows", machine="ARM64"), "x86_64"),  # Windows on ARM
        (FakeHost(system="Linux", machine="aarch64", os_id="ubuntu", os_version="24.04"), "x86_64"),
        (FakeHost(system="Windows", machine="AMD64", version="10.0.19045"), "Windows 11"),  # Windows 10
        (FakeHost(system="Linux", machine="x86_64", os_id="debian", os_version="12"), "Ubuntu"),
    ],
)
def test_unsupported_hosts_fail_with_remediation(tmp_path, host):
    host, expected_text = host
    report, _ = run(tmp_path, host=host)
    check = by_name(report, "supported_platform")
    assert check.status is CheckStatus.FAIL
    assert expected_text in check.remediation
    assert not report.ok


# --- container runtime ------------------------------------------------------


def test_docker_missing_fails_with_platform_remediation(tmp_path):
    report, _ = run(tmp_path, docker=(1, ""))
    check = by_name(report, "container_runtime")
    assert check.status is CheckStatus.FAIL
    assert "Docker Desktop" in check.remediation  # macOS wording


def test_docker_missing_on_windows_requires_wsl2_remediation(tmp_path):
    report, _ = run(
        tmp_path,
        host=FakeHost(system="Windows", machine="AMD64", version="10.0.22631"),
        docker=(1, ""),
    )
    assert "WSL2" in by_name(report, "container_runtime").remediation


def test_docker_present_but_wsl2_missing_fails_on_windows(tmp_path):
    report, _ = run(
        tmp_path,
        host=FakeHost(system="Windows", machine="AMD64", version="10.0.22631"),
        docker=(0, ""),
        wsl=(1, ""),
    )
    check = by_name(report, "container_runtime")
    assert check.status is CheckStatus.FAIL
    assert "WSL2" in check.remediation


def test_docker_and_wsl2_present_passes_on_windows(tmp_path):
    report, _ = run(
        tmp_path,
        host=FakeHost(system="Windows", machine="AMD64", version="10.0.22631"),
        docker=(0, ""),
        wsl=(0, ""),
    )
    assert by_name(report, "container_runtime").status is CheckStatus.OK


# --- data paths -------------------------------------------------------------


def test_writable_data_paths_pass(tmp_path):
    report, _ = run(tmp_path)
    assert by_name(report, "data_paths_writable").status is CheckStatus.OK


def test_unwritable_data_path_fails(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory must go")
    settings = Settings.load(overrides={"data_dir": str(blocker), "backup_dir": str(tmp_path / "backups")})
    runner = FakeRunner({("docker", "info"): (0, "")})
    report = run_doctor(settings, host=FakeHost(system="Darwin", machine="arm64"), runner=runner, ollama=FakeOllama(["qwen3:8b", "bge-m3"]))
    assert by_name(report, "data_paths_writable").status is CheckStatus.FAIL


# --- ollama and models ------------------------------------------------------


def test_ollama_unreachable_fails_with_remediation(tmp_path):
    report, _ = run(tmp_path, ollama_reachable=False)
    check = by_name(report, "ollama_reachable")
    assert check.status is CheckStatus.FAIL
    assert "Ollama" in check.remediation
    assert not report.ok


def test_missing_models_fail_with_pull_command(tmp_path):
    report, _ = run(tmp_path, models=("qwen3:8b",))
    check = by_name(report, "models_present")
    assert check.status is CheckStatus.FAIL
    assert "ollama pull bge-m3" in check.remediation
    assert not report.ok


def test_all_models_present_pass(tmp_path):
    report, _ = run(tmp_path, models=("qwen3:8b", "bge-m3", "qwen3:4b"))
    assert by_name(report, "models_present").status is CheckStatus.OK


# --- profile reporting ------------------------------------------------------


def test_report_includes_active_model_profile(tmp_path):
    report, _settings = run(tmp_path)
    line = by_name(report, "active_profile").detail
    assert "recommended" in line
    assert "qwen3:8b" in line
    assert "bge-m3" in line
    assert "1024" in line


def test_compact_profile_is_reported(tmp_path):
    report, _ = run(tmp_path, profile="compact")
    assert "compact" in by_name(report, "active_profile").detail


# --- sanitized output -------------------------------------------------------


def test_report_render_never_contains_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", "sk-supersecret-value")
    report, _ = run(tmp_path)
    tainted = report.results[0]
    object.__setattr__(tainted, "detail", f"echo {report.secret_values()[0]} back")
    rendered = report.render()
    assert "sk-supersecret-value" not in rendered
    assert "[redacted]" in rendered
