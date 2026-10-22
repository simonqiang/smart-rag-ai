"""Pilot installation, LAN profile, support bundle, and release packaging CLI (Task 28).

Commands:
    install         verify host prerequisites (doctor), initialize directories, migrate schema, and start stack
    restart         restart containers without touching persistent data
    stop            stop containers without deleting persistent data or volumes
    lan-config      validate LAN TLS/host/origin/proxy settings and render nginx reverse-proxy config
    support-bundle  export a secret-redacted, passage-free diagnostic bundle zip
    package         verify immutable `make check-release` evidence for HEAD and emit a versioned release manifest
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import platform
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from foundation.config import Settings
from foundation.doctor import CheckStatus, DoctorReport, default_runner, run_doctor

CURRENT_SCHEMA_REVISION = "0015"
SUPPORTED_PLATFORMS = (
    "windows11-x86_64",
    "macos-apple-silicon",
    "ubuntu-lts-x86_64",
)


class InstallRefused(Exception):
    """Host prerequisites failed or a command step failed."""


class LanConfigError(Exception):
    """LAN reverse-proxy configuration violates TLS, host, origin, or IP policy."""


class PackageGateError(Exception):
    """Packaging is blocked because check-release evidence is missing or incomplete."""


@dataclass(frozen=True)
class InstallReceipt:
    status: str
    profile: str
    data_dir: str
    backup_dir: str
    installed_at: str


@dataclass(frozen=True)
class LanProfile:
    bind_address: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    tls_cert_path: str
    tls_key_path: str
    trusted_proxy: str
    port: int = 8443


def run_install(
    settings: Settings,
    *,
    doctor_fn: Callable[[Settings], DoctorReport] = run_doctor,
    runner: Callable[[list[str]], tuple[int, str]] = default_runner,
) -> InstallReceipt:
    """Validate host prerequisites, ensure data/backup dirs, apply schema, and start stack."""
    report = doctor_fn(settings)
    if not report.ok:
        failures = [
            f"{r.name}: {r.detail}" + (f" ({r.remediation})" if r.remediation else "")
            for r in report.results
            if r.status is CheckStatus.FAIL
        ]
        raise InstallRefused("doctor checks failed: " + "; ".join(failures))

    data_path = Path(settings.data_dir)
    backup_path = Path(settings.backup_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    backup_path.mkdir(parents=True, exist_ok=True)

    for step in (
        ["docker", "compose", "up", "-d", "--wait", "postgres", "redis"],
        ["uv", "run", "alembic", "upgrade", "head"],
        [
            "docker",
            "compose",
            "up",
            "-d",
            "--build",
            "--scale",
            "worker=1",
            "--scale",
            "dispatcher=1",
        ],
    ):
        code, out = runner(step)
        if code != 0:
            raise InstallRefused(f"command failed ({' '.join(step)}): {out.strip()}")

    receipt = InstallReceipt(
        status="installed",
        profile=settings.profile,
        data_dir=str(data_path),
        backup_dir=str(backup_path),
        installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    (data_path / "install-receipt.json").write_text(
        json.dumps(asdict(receipt), indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def restart_stack(
    _settings: Settings,
    *,
    runner: Callable[[list[str]], tuple[int, str]] = default_runner,
) -> None:
    """Restart services cleanly without deleting volumes or host directories."""
    code, out = runner(["docker", "compose", "restart"])
    if code != 0:
        raise InstallRefused(f"docker compose restart failed: {out.strip()}")


def stop_stack(
    _settings: Settings,
    *,
    runner: Callable[[list[str]], tuple[int, str]] = default_runner,
) -> None:
    """Stop services without deleting persistent volumes or host data directories."""
    code, out = runner(["docker", "compose", "down"])
    if code != 0:
        raise InstallRefused(f"docker compose down failed: {out.strip()}")


def validate_lan_profile(
    *,
    bind_address: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    tls_cert_path: Path,
    tls_key_path: Path,
    trusted_proxy: str = "172.16.0.0/12",
    port: int = 8443,
) -> LanProfile:
    """Validate explicit LAN exposure rules (spec §10 and system-architecture §8)."""
    if not tls_cert_path.is_file() or not tls_key_path.is_file():
        raise LanConfigError(
            "LAN mode requires existing TLS certificate and private key files"
        )

    try:
        addr = ipaddress.ip_address(bind_address)
    except ValueError as exc:
        raise LanConfigError(f"invalid bind IP address {bind_address!r}") from exc
    if addr.is_unspecified or not (addr.is_private or addr.is_loopback):
        raise LanConfigError(
            f"bind address {bind_address} must be a private LAN or loopback IP; "
            "public/wildcard binding is refused"
        )

    cleaned_hosts = tuple(h.strip().lower() for h in allowed_hosts if h.strip())
    if not cleaned_hosts or any("*" in h or " " in h for h in cleaned_hosts):
        raise LanConfigError(
            "allowed_hosts must contain explicit hostnames without wildcards"
        )

    cleaned_origins = tuple(o.strip() for o in allowed_origins if o.strip())
    if not cleaned_origins:
        raise LanConfigError("allowed_origins must list at least one https:// origin")
    for origin in cleaned_origins:
        parsed = urlparse(origin)
        if parsed.scheme != "https" or not parsed.hostname or "*" in parsed.hostname:
            raise LanConfigError(
                f"LAN origin {origin!r} must use https:// and an explicit hostname"
            )

    try:
        ipaddress.ip_network(trusted_proxy, strict=False)
    except ValueError as exc:
        raise LanConfigError(f"invalid trusted_proxy CIDR {trusted_proxy!r}") from exc

    return LanProfile(
        bind_address=bind_address,
        allowed_hosts=cleaned_hosts,
        allowed_origins=cleaned_origins,
        tls_cert_path=str(tls_cert_path),
        tls_key_path=str(tls_key_path),
        trusted_proxy=trusted_proxy,
        port=port,
    )


def render_lan_nginx_config(profile: LanProfile) -> str:
    """Render an explicit TLS reverse-proxy configuration for LAN mode."""
    hosts = " ".join(profile.allowed_hosts)
    origins_regex = "|".join(
        origin.replace(".", r"\.") for origin in profile.allowed_origins
    )
    return (
        f"# Generated LAN TLS reverse-proxy profile ({profile.bind_address}:{profile.port})\n"
        f"map $http_origin $cors_origin {{\n"
        f"    default \"\";\n"
        f"    ~^({origins_regex})$ $http_origin;\n"
        f"}}\n\n"
        f"server {{\n"
        f"    listen 443 ssl;\n"
        f"    server_name {hosts};\n\n"
        f"    ssl_certificate {profile.tls_cert_path};\n"
        f"    ssl_certificate_key {profile.tls_key_path};\n"
        f"    ssl_protocols TLSv1.2 TLSv1.3;\n\n"
        f"    set_real_ip_from {profile.trusted_proxy};\n"
        f"    real_ip_header X-Forwarded-For;\n"
        f"    real_ip_recursive on;\n\n"
        f"    root /usr/share/nginx/html;\n"
        f"    index index.html;\n"
        f"    resolver 127.0.0.11 valid=10s ipv6=off;\n\n"
        f"    location /api/ {{\n"
        f"        set $api_upstream http://api:8000;\n"
        f"        proxy_pass $api_upstream;\n"
        f"        client_max_body_size 51m;\n"
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Host $host;\n"
        f"        proxy_set_header X-Real-IP $remote_addr;\n"
        f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"        proxy_set_header X-Forwarded-Proto https;\n"
        f"        proxy_cookie_flags ~ secure httponly samesite=strict;\n"
        f"        add_header Set-Cookie \"Path=/; HttpOnly; Secure; SameSite=Strict\" always;\n"
        f"        add_header Access-Control-Allow-Origin $cors_origin always;\n"
        f"        proxy_buffering off;\n"
        f"        proxy_read_timeout 300s;\n"
        f"    }}\n\n"
        f"    location / {{\n"
        f"        try_files $uri $uri/ /index.html;\n"
        f"    }}\n"
        f"}}\n"
    )


async def export_support_bundle(
    settings: Settings,
    *,
    output_path: Path,
    doctor_report: DoctorReport | None = None,
) -> Path:
    """Export a secret-redacted, passage-free diagnostic support bundle."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    report = doctor_report or run_doctor(settings)
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            counts = {
                table: (
                    await conn.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one()
                for table in (
                    "workspaces",
                    "users",
                    "collections",
                    "sources",
                    "source_versions",
                    "index_generations",
                    "index_chunks",
                    "jobs",
                    "managed_backups",
                )
            }
            job_rows = (
                await conn.execute(
                    text(
                        "SELECT id, type, status, attempts, last_error, created_at, updated_at "
                        "FROM jobs ORDER BY created_at DESC LIMIT 100"
                    )
                )
            ).mappings().all()
            audit_rows = (
                await conn.execute(
                    text(
                        "SELECT id, type, actor, subject, metadata, created_at "
                        "FROM audit_events ORDER BY created_at DESC LIMIT 100"
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    def _clean(text_val: str) -> str:
        return report.redact(text_val)

    manifest = {
        "bundle": "smart-rag-support",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "profile": settings.profile,
        "schema_revision": CURRENT_SCHEMA_REVISION,
    }
    jobs_payload = [
        {
            "id": str(r["id"]),
            "type": r["type"],
            "status": r["status"],
            "attempts": r["attempts"],
            "last_error": _clean(str(r["last_error"])) if r["last_error"] else None,
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in job_rows
    ]
    audit_payload = [
        {
            "id": str(r["id"]),
            "type": r["type"],
            "actor": _clean(str(r["actor"])),
            "subject": _clean(str(r["subject"])),
            "metadata": json.loads(_clean(json.dumps(r["metadata"]))),
            "created_at": r["created_at"].isoformat(),
        }
        for r in audit_rows
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        zf.writestr("doctor.txt", report.render() + "\n")
        zf.writestr("counts.json", json.dumps(counts, indent=2) + "\n")
        zf.writestr("jobs.json", json.dumps(jobs_payload, indent=2) + "\n")
        zf.writestr("audit.json", json.dumps(audit_payload, indent=2) + "\n")
    return output_path


def build_release_package(
    *,
    revision: str,
    build_dir: Path,
    output_dir: Path,
) -> Path:
    """Verify `make check-release` evidence for `revision` and emit a package manifest."""
    evidence_path = build_dir / f"evidence-{revision}.json"
    if not evidence_path.is_file():
        raise PackageGateError(
            f"missing release evidence {evidence_path}; run 'make check-release' on {revision} first"
        )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    gates = set(evidence.get("gates", []))
    if (
        evidence.get("revision") != revision
        or evidence.get("result") != "PASS"
        or "test-e2e-full" not in gates
    ):
        raise PackageGateError(
            f"evidence {evidence_path} does not record a passing 'test-e2e-full' release gate"
        )

    eval_path = build_dir / "evaluation-report.json"
    if not eval_path.is_file():
        raise PackageGateError(
            f"missing evaluation report {eval_path}; run 'make check-release' first"
        )

    settings = Settings.load()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package": "smart-rag-ai-pilot",
        "version": "0.1.0",
        "revision": revision,
        "schema_revision": CURRENT_SCHEMA_REVISION,
        "packaged_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "evidence_verified": True,
        "evidence_file": str(evidence_path.name),
        "evaluation_report": str(eval_path.name),
        "supported_platforms": list(SUPPORTED_PLATFORMS),
        "profiles": {
            name: asdict(profile) for name, profile in settings.profiles.items()
        },
        "rollback_procedure": "python infra/scripts/upgrade.py rollback --to-revision <prior-sha> --backup <file>",
    }
    manifest_path = output_dir / f"release-manifest-{revision}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _git_short_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smart RAG AI pilot installer & packager")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="validate prerequisites, migrate, and start stack")
    sub.add_parser("restart", help="restart containers without touching data")
    sub.add_parser("stop", help="stop containers without deleting data")
    bundle_p = sub.add_parser("support-bundle", help="export a redacted support zip")
    bundle_p.add_argument(
        "--output",
        type=Path,
        default=Path("build") / "support-bundle.zip",
    )
    pkg_p = sub.add_parser("package", help="verify check-release evidence and emit release manifest")
    pkg_p.add_argument("--revision", default=None)
    pkg_p.add_argument("--build-dir", type=Path, default=Path("build"))
    pkg_p.add_argument("--output-dir", type=Path, default=Path("build") / "release")

    args = parser.parse_args(argv)
    settings = Settings.load()
    try:
        if args.command == "install":
            receipt = run_install(settings)
            print(f"installed ({receipt.profile}) -> {receipt.data_dir}")
        elif args.command == "restart":
            restart_stack(settings)
            print("stack restarted (data preserved)")
        elif args.command == "stop":
            stop_stack(settings)
            print("stack stopped (data preserved)")
        elif args.command == "support-bundle":
            out = asyncio.run(export_support_bundle(settings, output_path=args.output))
            print(f"support bundle: {out}")
        elif args.command == "package":
            rev = args.revision or _git_short_head()
            manifest_path = build_release_package(
                revision=rev, build_dir=args.build_dir, output_dir=args.output_dir
            )
            print(f"release manifest: {manifest_path}")
    except (InstallRefused, LanConfigError, PackageGateError) as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
