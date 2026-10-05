"""Integration smoke (Task 3): settings load end to end from the YAML file."""

from foundation.config import Settings


def test_settings_load_from_committed_config() -> None:
    settings = Settings.load()
    assert settings.profile == "recommended"
    assert settings.active_profile.chat_model == "qwen3:8b"
    assert settings.limits.upload_mb == 50
