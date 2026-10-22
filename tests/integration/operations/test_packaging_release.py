"""Task 28: pilot packaging, install, upgrade, support bundle, LAN profile, and release verification."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from foundation.config import Settings
from foundation.doctor import CheckResult, CheckStatus, DoctorReport
from foundation.jobs import JobClaims, JobCommand, JobQueue, Lease
from foundation.unit_of_work import UnitOfWork

ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = ROOT / "infra" / "scripts" / "install.py"
UPGRADE_SCRIPT = ROOT / "infra" / "scripts" / "upgrade.py"
OPERATOR_GUIDE = ROOT / "docs" / "operator-guide.md"
RELEASE_CHECKLIST = ROOT / "docs" / "release-checklist.md"
MAKEFILE = ROOT / "Makefile"


def _load_module(name: str, path: Path):
    import sys

    assert path.is_file(), f"missing script: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def install():
    return _load_module("install_script", INSTALL_SCRIPT)


@pytest.fixture(scope="module")
def upgrade():
    return _load_module("upgrade_script", UPGRADE_SCRIPT)


def _ok_doctor(secrets: list[str] | None = None) -> DoctorReport:
    return DoctorReport(
        host="Darwin/arm64",
        results=[
            CheckResult("supported_platform", CheckStatus.OK, "macOS on Apple Silicon"),
            CheckResult("container_runtime", CheckStatus.OK, "Docker is available"),
            CheckResult("data_paths_writable", CheckStatus.OK, "writable"),
            CheckResult("ocr_language_packs", CheckStatus.OK, "eng, chi_sim, chi_tra, msa"),
            CheckResult("active_profile", CheckStatus.OK, "profile 'recommended'"),
            CheckResult("ollama_reachable", CheckStatus.OK, "Ollama reachable"),
            CheckResult("models_present", CheckStatus.OK, "profile models are pulled"),
        ],
        secrets=secrets or [],
    )


def _failing_doctor() -> DoctorReport:
    return DoctorReport(
        host="Windows/AMD64",
        results=[
            CheckResult(
                "container_runtime",
                CheckStatus.FAIL,
                "Docker is available but WSL2 is not",
                "Docker Desktop requires WSL2: run `wsl --install` as administrator, then restart Docker Desktop.",
            )
        ],
        secrets=[],
    )


# --- First install, restart, and uninstall without data loss -----------------


def test_first_install_runs_doctor_creates_dirs_migrates_and_starts_stack(
    install, tmp_path: Path
) -> None:
    settings = Settings.load(
        overrides={
            "data_dir": str(tmp_path / "data"),
            "backup_dir": str(tmp_path / "backups"),
        }
    )
    commands: list[list[str]] = []

    def fake_runner(args: list[str]) -> tuple[int, str]:
        commands.append(list(args))
        return 0, "ok"

    receipt = install.run_install(
        settings,
        doctor_fn=lambda _s: _ok_doctor(),
        runner=fake_runner,
    )

    assert receipt.status == "installed"
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "backups").is_dir()
    assert any("upgrade" in cmd and "head" in cmd for cmd in commands)
    assert any(cmd[:3] == ["docker", "compose", "up"] for cmd in commands)
    receipt_file = Path(settings.data_dir) / "install-receipt.json"
    assert receipt_file.is_file()
    persisted = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert persisted["profile"] == "recommended"


def test_first_install_refuses_when_doctor_fails(install, tmp_path: Path) -> None:
    settings = Settings.load(
        overrides={
            "data_dir": str(tmp_path / "data"),
            "backup_dir": str(tmp_path / "backups"),
        }
    )
    with pytest.raises(install.InstallRefused) as excinfo:
        install.run_install(
            settings,
            doctor_fn=lambda _s: _failing_doctor(),
            runner=lambda _args: (0, ""),
        )
    assert "WSL2" in str(excinfo.value)


def test_restart_and_uninstall_preserve_persistent_data_by_default(
    install, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir()
    backup_dir.mkdir()
    sentinel = data_dir / "objects" / "keep.bin"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"knowledge-bytes")

    settings = Settings.load(
        overrides={"data_dir": str(data_dir), "backup_dir": str(backup_dir)}
    )
    commands: list[list[str]] = []

    def fake_runner(args: list[str]) -> tuple[int, str]:
        commands.append(list(args))
        return 0, "ok"

    install.restart_stack(settings, runner=fake_runner)
    install.stop_stack(settings, runner=fake_runner)

    # Stop/uninstall never passes -v/--volumes or deletes host directories.
    for cmd in commands:
        assert "-v" not in cmd
        assert "--volumes" not in cmd
    assert sentinel.read_bytes() == b"knowledge-bytes"


# --- LAN TLS / proxy / host / origin / cookie policy -------------------------


def test_lan_profile_enforces_tls_host_origin_cookie_and_proxy_policy(
    install, tmp_path: Path
) -> None:
    cert = tmp_path / "lan.crt"
    key = tmp_path / "lan.key"
    cert.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    key.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")

    # Missing TLS cert/key or wildcard/public bind is refused.
    with pytest.raises(install.LanConfigError, match="TLS"):
        install.validate_lan_profile(
            bind_address="192.168.1.50",
            allowed_hosts=["rag.office.internal"],
            allowed_origins=["https://rag.office.internal"],
            tls_cert_path=tmp_path / "missing.crt",
            tls_key_path=key,
        )

    with pytest.raises(install.LanConfigError, match="private|LAN|loopback"):
        install.validate_lan_profile(
            bind_address="0.0.0.0",
            allowed_hosts=["rag.office.internal"],
            allowed_origins=["https://rag.office.internal"],
            tls_cert_path=cert,
            tls_key_path=key,
        )

    with pytest.raises(install.LanConfigError, match="wildcard|host"):
        install.validate_lan_profile(
            bind_address="192.168.1.50",
            allowed_hosts=["*"],
            allowed_origins=["https://rag.office.internal"],
            tls_cert_path=cert,
            tls_key_path=key,
        )

    with pytest.raises(install.LanConfigError, match="https"):
        install.validate_lan_profile(
            bind_address="192.168.1.50",
            allowed_hosts=["rag.office.internal"],
            allowed_origins=["http://rag.office.internal"],
            tls_cert_path=cert,
            tls_key_path=key,
        )

    profile = install.validate_lan_profile(
        bind_address="192.168.1.50",
        allowed_hosts=["rag.office.internal"],
        allowed_origins=["https://rag.office.internal"],
        tls_cert_path=cert,
        tls_key_path=key,
        trusted_proxy="172.18.0.0/16",
    )
    rendered = install.render_lan_nginx_config(profile)
    assert "ssl_certificate" in rendered
    assert "rag.office.internal" in rendered
    assert "HttpOnly; Secure; SameSite=Strict" in rendered
    assert "set_real_ip_from 172.18.0.0/16;" in rendered


# --- Support bundle export & diagnostics redaction ---------------------------


def test_support_bundle_redacts_secrets_and_omits_source_passages(
    install, db: Settings, workspace: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_key = "sk-live-super-secret-glm-999"
    backup_key = "backup-passphrase-secret-888"
    sensitive_passage = "CONFIDENTIAL_BOARD_PASSAGE_DO_NOT_EXPORT_777"
    monkeypatch.setenv("SMART_RAG_GLM_API_KEY", secret_key)
    monkeypatch.setenv("SMART_RAG_BACKUP_KEY", backup_key)

    settings = Settings.load(
        overrides={
            "data_dir": str(tmp_path / "data"),
            "backup_dir": str(tmp_path / "backups"),
        }
    )

    async def seed_chunk() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            source_id = str(uuid.uuid4())
            version_id = str(uuid.uuid4())
            gen_id = str(uuid.uuid4())
            ws = workspace["workspace_id"]
            owner = workspace["owner_id"]
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO sources (id, workspace_id, collection_id, name, state,"
                        " created_by, created_at, updated_at) VALUES "
                        "(:id, :ws, :col, 'Secret.pdf', 'active', :owner, now(), now())"
                    ),
                    {"id": source_id, "ws": ws, "col": workspace["collection_id"], "owner": owner},
                )
                await conn.execute(
                    text(
                        "INSERT INTO source_versions (id, source_id, workspace_id, media_type,"
                        " filename, object_sha256, size_bytes, state, created_by, created_at,"
                        " updated_at) VALUES (:id, :sid, :ws, 'application/pdf', 's.pdf',"
                        " 'abc', 12, 'active', :owner, now(), now())"
                    ),
                    {"id": version_id, "sid": source_id, "ws": ws, "owner": owner},
                )
                await conn.execute(
                    text(
                        "INSERT INTO index_generations (id, workspace_id, source_version_id,"
                        " compatibility_key, dimension, model, state, chunk_count,"
                        " activated_at, created_at) VALUES "
                        "(:id, :ws, :vid, 'bge-m3:1024:unigrams', 3, 'bge-m3', 'active', 1,"
                        " now(), now())"
                    ),
                    {"id": gen_id, "ws": ws, "vid": version_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO index_chunks (id, generation_id, workspace_id, source_id,"
                        " source_version_id, ordinal, text, language, embedding, keywords,"
                        " block_start, block_end, created_at) VALUES "
                        "(:id, :gid, :ws, :sid, :vid, 0, :passage, 'en',"
                        " CAST('[0.1, 0.2, 0.3]' AS vector), to_tsvector('english', 'secret'),"
                        " 0, 0, now())"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "gid": gen_id,
                        "ws": ws,
                        "sid": source_id,
                        "vid": version_id,
                        "passage": sensitive_passage,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_chunk())

    tainted_doctor = DoctorReport(
        host="Darwin/arm64",
        results=[
            CheckResult(
                "ollama_reachable",
                CheckStatus.WARN,
                f"key={secret_key} backup={backup_key}",
            )
        ],
        secrets=settings.secret_values(),
    )
    bundle_path = tmp_path / "support-bundle.zip"
    asyncio.run(
        install.export_support_bundle(
            settings,
            output_path=bundle_path,
            doctor_report=tainted_doctor,
        )
    )

    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "doctor.txt", "counts.json", "jobs.json", "audit.json"} <= names
        combined = "\n".join(
            archive.read(name).decode("utf-8") for name in names
        )
    assert secret_key not in combined
    assert backup_key not in combined
    assert sensitive_passage not in combined
    assert "[redacted]" in combined


# --- Safe upgrade, unsafe downgrade refusal, rollback, graceful shutdown -----


def test_schema_compatibility_check_allows_same_or_older_and_refuses_downgrade(
    upgrade, db: Settings
) -> None:
    # Current head is 0015: matching head is compatible; older DB is upgradable.
    state = asyncio.run(upgrade.check_schema_compatibility(db, expected_head="0015"))
    assert state.compatible is True
    assert state.current_revision == "0015"
    assert state.needs_migration is False

    older = asyncio.run(
        upgrade.check_schema_compatibility(
            db, expected_head="0015", current_override="0014"
        )
    )
    assert older.compatible is True
    assert older.needs_migration is True

    # Unsafe downgrade (DB is at a newer revision than this code supports) is refused.
    with pytest.raises(upgrade.UnsafeDowngradeRefused) as excinfo:
        asyncio.run(
            upgrade.check_schema_compatibility(
                db, expected_head="0014", current_override="0015"
            )
        )
    assert "0015" in str(excinfo.value)
    assert "0014" in str(excinfo.value)


def test_graceful_shutdown_recovers_in_progress_jobs_to_pending(
    upgrade, db: Settings
) -> None:
    async def scenario() -> tuple[int, str, str | None]:
        engine = create_async_engine(db.database_url)
        try:
            async with UnitOfWork(engine).transaction() as tx:
                job_id = await JobQueue().enqueue(
                    JobCommand(type="extract_document", payload={"source_id": "s1"}),
                    tx,
                )
            lease = await JobClaims(engine).claim(job_id, lease_seconds=300)
            assert isinstance(lease, Lease)
        finally:
            await engine.dispose()

        recovered = await upgrade.drain_in_progress_jobs(db, actor="host:upgrade")

        engine2 = create_async_engine(db.database_url)
        try:
            async with engine2.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT status, lease_owner FROM jobs WHERE id = CAST(:id AS uuid)"
                        ),
                        {"id": str(job_id)},
                    )
                ).one()
            return recovered, row.status, row.lease_owner
        finally:
            await engine2.dispose()

    recovered, status, lease_owner = asyncio.run(scenario())
    assert recovered == 1
    assert status == "pending"
    assert lease_owner is None


def test_safe_upgrade_preserves_data_and_failed_upgrade_rolls_back(
    upgrade, db: Settings, workspace: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMART_RAG_BACKUP_KEY", "upgrade-recovery-key-123")
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir()
    backup_dir.mkdir()
    settings = Settings.load(
        overrides={"data_dir": str(data_dir), "backup_dir": str(backup_dir)}
    )

    result = asyncio.run(
        upgrade.run_upgrade(
            settings,
            target_revision="0015",
            migrate_fn=lambda _s, _rev: None,
            actor="host:operator",
        )
    )
    assert result.status == "upgraded"
    assert result.backup_filename.endswith(".srb")
    assert (backup_dir / result.backup_filename).is_file()

    # Data in workspace is preserved across upgrade.
    async def count_users() -> int:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
        finally:
            await engine.dispose()

    assert asyncio.run(count_users()) == 1

    # Failed migration step records rollback instructions pointing to the pre-upgrade backup.
    def failing_migrate(_s: Settings, _rev: str) -> None:
        raise RuntimeError("simulated migration failure")

    with pytest.raises(upgrade.UpgradeFailed) as excinfo:
        asyncio.run(
            upgrade.run_upgrade(
                settings,
                target_revision="0015",
                migrate_fn=failing_migrate,
                actor="host:operator",
            )
        )
    assert excinfo.value.backup_filename.endswith(".srb")
    assert "make restore" in str(excinfo.value)


# --- Package manifest & release evidence gate --------------------------------


def test_package_release_requires_matching_check_release_evidence(
    install, tmp_path: Path
) -> None:
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    # Missing evidence -> refused.
    with pytest.raises(install.PackageGateError, match="check-release"):
        install.build_release_package(
            revision="abc1234",
            build_dir=build_dir,
            output_dir=tmp_path / "dist",
        )

    # Incomplete evidence (missing test-e2e-full) -> refused.
    (build_dir / "evidence-abc1234.json").write_text(
        json.dumps(
            {
                "revision": "abc1234",
                "timestamp": "2026-10-23T00:00:00+00:00",
                "platform": "macOS-arm64",
                "python": "3.13.0",
                "result": "PASS",
                "gates": ["check"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(install.PackageGateError, match="test-e2e-full"):
        install.build_release_package(
            revision="abc1234",
            build_dir=build_dir,
            output_dir=tmp_path / "dist",
        )

    # Complete check-release evidence + evaluation report -> produces versioned package manifest.
    (build_dir / "evidence-abc1234.json").write_text(
        json.dumps(
            {
                "revision": "abc1234",
                "timestamp": "2026-10-23T00:00:00+00:00",
                "platform": "macOS-arm64",
                "python": "3.13.0",
                "result": "PASS",
                "gates": ["check", "test-e2e-full"],
            }
        ),
        encoding="utf-8",
    )
    (build_dir / "evaluation-report.json").write_text(
        json.dumps({"passed": True, "strata": {"en": {"leakage_count": 0}}}),
        encoding="utf-8",
    )

    manifest_path = install.build_release_package(
        revision="abc1234",
        build_dir=build_dir,
        output_dir=tmp_path / "dist",
    )
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["revision"] == "abc1234"
    assert manifest["schema_revision"] == "0015"
    assert manifest["evidence_verified"] is True
    assert set(manifest["supported_platforms"]) == {
        "windows11-x86_64",
        "macos-apple-silicon",
        "ubuntu-lts-x86_64",
    }


# --- Documentation & Makefile contracts --------------------------------------


def test_operator_guide_release_checklist_and_makefile_targets_exist() -> None:
    assert OPERATOR_GUIDE.is_file(), "docs/operator-guide.md must exist"
    assert RELEASE_CHECKLIST.is_file(), "docs/release-checklist.md must exist"

    guide = OPERATOR_GUIDE.read_text(encoding="utf-8")
    # Per-platform prerequisites, Docker Desktop licence terms, Ollama, LAN, backup/restore, upgrade/rollback, model profiles, known limitations.
    for required in (
        "Windows 11",
        "WSL2",
        "Docker Desktop",
        "subscription",
        "macOS",
        "Apple Silicon",
        "Ubuntu",
        "22.04",
        "24.04",
        "Ollama",
        "qwen3:8b",
        "qwen3:4b",
        "bge-m3",
        "make setup",
        "make up",
        "make down",
        "make doctor",
        "make backup",
        "make restore",
        "make reset-owner-password",
        "LAN",
        "TLS",
        "rollback",
        "Known limitations",
    ):
        assert required in guide, f"operator-guide.md missing {required!r}"

    checklist = RELEASE_CHECKLIST.read_text(encoding="utf-8")
    for required in (
        "make check-release",
        "Windows 11",
        "macOS",
        "Ubuntu",
        "Playwright",
        "evaluation-report.json",
        "rollback",
        "design-partner",
        "support-bundle",
    ):
        assert required in checklist, f"release-checklist.md missing {required!r}"

    makefile = MAKEFILE.read_text(encoding="utf-8")
    for target in (
        "setup:",
        "up:",
        "down:",
        "doctor:",
        "backup:",
        "restore:",
        "reset-owner-password:",
        "check:",
        "check-release:",
        "package:",
        "upgrade:",
        "support-bundle:",
    ):
        assert target in makefile, f"Makefile missing target {target!r}"
