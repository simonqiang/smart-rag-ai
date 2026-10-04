"""Runtime diagnostics for the supported local-first profile.

Task 1 contract: ``run_doctor(settings) -> DoctorReport``. Host, command, and
Ollama dependencies are injectable so tests are deterministic on any machine;
the defaults probe the real system for ``make doctor``.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from foundation.config import Settings

WINDOWS_MIN_BUILD = 22000  # Windows 11 builds start at 22000
UBUNTU_LTS_VERSIONS = ("22.04", "24.04")


class CheckStatus(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    remediation: str = ""


@dataclass
class HostInfo:
    system: str  # "Windows" | "Darwin" | "Linux"
    machine: str  # "AMD64" | "ARM64" | "arm64" | "x86_64" | "aarch64"
    version: str = ""
    os_id: str = ""
    os_version: str = ""


def _windows_build(version: str) -> int:
    try:
        return int(version.split(".")[-1])
    except ValueError:
        return 0


@dataclass
class DoctorReport:
    host: str
    results: list[CheckResult]
    secrets: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status is not CheckStatus.FAIL for r in self.results)

    def secret_values(self) -> list[str]:
        return self.secrets

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def render(self) -> str:
        lines = [f"host: {self.host}"]
        for r in self.results:
            lines.append(f"[{r.status.value.upper():4}] {r.name}: {self.redact(r.detail)}")
            if r.remediation and r.status is not CheckStatus.OK:
                lines.append(f"       fix: {self.redact(r.remediation)}")
        lines.append("doctor: PASS" if self.ok else "doctor: FAIL")
        return "\n".join(lines)


def probe_host() -> HostInfo:
    os_id = os_version = ""
    if platform.system() == "Linux":
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key == "ID":
                    os_id = value.strip().strip('"')
                elif key == "VERSION_ID":
                    os_version = value.strip().strip('"')
        except OSError:
            pass
    return HostInfo(
        system=platform.system(),
        machine=platform.machine(),
        version=platform.version(),
        os_id=os_id,
        os_version=os_version,
    )


def default_runner(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


class OllamaProbe:
    def __init__(self, host: str):
        self._host = host.rstrip("/")

    def list_models(self) -> list[str]:
        with urllib.request.urlopen(f"{self._host}/api/tags", timeout=5) as response:
            payload = json.load(response)
        return [m["name"] for m in payload.get("models", [])]


def _platform_check(host: HostInfo) -> CheckResult:
    if host.system == "Darwin":
        if host.machine == "arm64":
            return CheckResult("supported_platform", CheckStatus.OK, "macOS on Apple Silicon")
        return CheckResult(
            "supported_platform", CheckStatus.FAIL, f"unsupported Mac: {host.machine}",
            "Intel Macs are not supported for the MVP; use a Mac with Apple Silicon (macOS 14+).",
        )
    if host.system == "Windows":
        if host.machine != "AMD64":
            return CheckResult(
                "supported_platform", CheckStatus.FAIL, f"unsupported Windows architecture: {host.machine}",
                "Windows on ARM is not supported for the MVP; use Windows 11 x86_64.",
            )
        if _windows_build(host.version) < WINDOWS_MIN_BUILD:
            return CheckResult(
                "supported_platform", CheckStatus.FAIL, f"unsupported Windows build: {host.version}",
                "Upgrade to Windows 11 x86_64 (build 22000 or later).",
            )
        return CheckResult("supported_platform", CheckStatus.OK, "Windows 11 x86_64")
    if host.system == "Linux":
        if host.machine != "x86_64":
            return CheckResult(
                "supported_platform", CheckStatus.FAIL, f"unsupported Linux architecture: {host.machine}",
                "Linux arm64 is not supported for the MVP; use Ubuntu 22.04/24.04 LTS on x86_64.",
            )
        if host.os_id != "ubuntu" or host.os_version not in UBUNTU_LTS_VERSIONS:
            return CheckResult(
                "supported_platform", CheckStatus.FAIL,
                f"unsupported distribution: {host.os_id or 'unknown'} {host.os_version}".strip(),
                "Use Ubuntu 22.04 or 24.04 LTS on x86_64.",
            )
        return CheckResult("supported_platform", CheckStatus.OK, f"Ubuntu {host.os_version} LTS x86_64")
    return CheckResult(
        "supported_platform", CheckStatus.FAIL, f"unsupported system: {host.system}",
        "Supported platforms: Windows 11 x86_64, macOS 14+ Apple Silicon, Ubuntu 22.04/24.04 LTS x86_64.",
    )


def _runtime_remediation(host: HostInfo) -> str:
    if host.system == "Windows":
        return "Install Docker Desktop with the WSL2 backend: run `wsl --install`, then install Docker Desktop and enable 'Use the WSL 2 based engine'."
    if host.system == "Darwin":
        return "Install Docker Desktop for Apple Silicon from https://www.docker.com/products/docker-desktop/ and start it."
    return "Install Docker Engine and the Compose plugin: sudo apt-get install docker.io docker-compose-v2."


def _runtime_check(host: HostInfo, runner) -> CheckResult:
    returncode, _ = runner(["docker", "info"])
    if returncode != 0:
        return CheckResult("container_runtime", CheckStatus.FAIL, "Docker is not available", _runtime_remediation(host))
    if host.system == "Windows":
        wsl_code, _ = runner(["wsl.exe", "--status"])
        if wsl_code != 0:
            return CheckResult(
                "container_runtime", CheckStatus.FAIL, "Docker is available but WSL2 is not",
                "Docker Desktop requires WSL2: run `wsl --install` as administrator, then restart Docker Desktop.",
            )
    return CheckResult("container_runtime", CheckStatus.OK, "Docker is available")


def _paths_check(settings: Settings) -> CheckResult:
    for attr in ("data_dir", "backup_dir"):
        path = Path(getattr(settings, attr))
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return CheckResult(
                "data_paths_writable", CheckStatus.FAIL, f"{attr} not writable: {path} ({exc})",
                f"Create the directory or fix permissions for {path}.",
            )
    return CheckResult("data_paths_writable", CheckStatus.OK, f"{settings.data_dir} and {settings.backup_dir} writable")


def _model_matches(pulled: list[str], tag: str) -> bool:
    return any(name == tag or name.startswith(tag + ":") for name in pulled)


def run_doctor(
    settings: Settings,
    *,
    host: HostInfo | None = None,
    runner=None,
    ollama=None,
) -> DoctorReport:
    host = host or probe_host()
    runner = runner or default_runner
    ollama = ollama or OllamaProbe(settings.ollama_host)

    results = [_platform_check(host), _runtime_check(host, runner), _paths_check(settings)]

    profile = settings.active_profile
    results.append(CheckResult(
        "active_profile", CheckStatus.OK,
        f"profile '{profile.name}': chat={profile.chat_model}, "
        f"embedding={profile.embedding_model} ({profile.embedding_dimensions}d), "
        f"upload<={settings.limits.upload_mb}MB, pages<={settings.limits.max_pages_per_document}",
    ))

    try:
        pulled = ollama.list_models()
    except OSError as exc:
        results.append(CheckResult(
            "ollama_reachable", CheckStatus.FAIL, f"Ollama not reachable at {settings.ollama_host}: {exc}",
            f"Install Ollama from https://ollama.com (native app on {host.system}) and ensure it serves {settings.ollama_host}.",
        ))
        results.append(CheckResult(
            "models_present", CheckStatus.FAIL, "skipped: Ollama unreachable",
            "Start Ollama, then run: ollama pull " + profile.chat_model + " && ollama pull " + profile.embedding_model,
        ))
    else:
        results.append(CheckResult("ollama_reachable", CheckStatus.OK, f"Ollama reachable at {settings.ollama_host}"))
        missing = [
            model
            for model in (profile.chat_model, profile.embedding_model)
            if not _model_matches(pulled, model)
        ]
        if missing:
            results.append(CheckResult(
                "models_present", CheckStatus.FAIL, f"missing models: {', '.join(missing)}",
                "Run: " + " && ".join(f"ollama pull {m}" for m in missing),
            ))
        else:
            results.append(CheckResult("models_present", CheckStatus.OK, "profile models are pulled"))

    return DoctorReport(
        host=f"{host.system}/{host.machine}", results=results, secrets=settings.secret_values()
    )


def main() -> int:
    report = run_doctor(Settings.load())
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
