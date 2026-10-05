"""Task 4: environment overrides and connection URLs for the Compose stack."""

from foundation.config import Settings


def test_env_overrides_config_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SMART_RAG_PROFILE", "compact")
    monkeypatch.setenv("SMART_RAG_OLLAMA_HOST", "http://host.docker.internal:11434")
    monkeypatch.setenv("SMART_RAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMART_RAG_BACKUP_DIR", str(tmp_path / "backups"))

    settings = Settings.load()

    assert settings.profile == "compact"
    assert settings.ollama_host == "http://host.docker.internal:11434"
    assert settings.data_dir == str(tmp_path / "data")
    assert settings.backup_dir == str(tmp_path / "backups")


def test_connection_urls_default_to_local_compose_topology() -> None:
    settings = Settings.load()

    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url.startswith("redis://")


def test_database_password_is_reported_as_secret(monkeypatch) -> None:
    monkeypatch.setenv(
        "SMART_RAG_DATABASE_URL",
        "postgresql+asyncpg://smart_rag:hunter2@127.0.0.1:54329/smart_rag",
    )
    settings = Settings.load()

    assert "hunter2" in settings.secret_values()
