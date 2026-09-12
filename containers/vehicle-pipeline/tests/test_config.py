import json

import pytest

from vehicle_pipeline.config import Settings


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "PAPERLESS_URL": "https://paperless.example.test/",
        "PAPERLESS_TOKEN": "ptoken",
        "PAPERLESS_WEBHOOK_SECRET": "whsecret",
        "LITELLM_BASE_URL": "http://litellm.internal/v1",
        "LITELLM_API_KEY": "llmkey",
        "MYGARAGE_URL": "https://mygarage.example.test/",
        "MYGARAGE_USERNAME": "admin",
        "MYGARAGE_PASSWORD": "adminpw",
        "MYGARAGE_VEHICLES": json.dumps({"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"}),
    }
    base.update(overrides)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_from_env_reads_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = Settings.from_env()
    assert settings.paperless_url == "https://paperless.example.test"
    assert settings.vehicles == {"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"}
    assert settings.poll_interval_seconds == 900


def test_from_env_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("PAPERLESS_TOKEN")
    with pytest.raises(RuntimeError, match="PAPERLESS_TOKEN"):
        Settings.from_env()


def test_poll_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, POLL_INTERVAL="5m")
    assert Settings.from_env().poll_interval_seconds == 300
