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
        # Real shape (confirmed live 2026-09-12): a list of rich vehicle
        # objects shared with WiCAN/trip-enricher, not a plain {vin: label}
        # map -- Settings.from_env() must extract vin -> "id4"/"multivan"
        # from this.
        "MYGARAGE_VEHICLES": json.dumps([
            {"vin": "WV2ZZZST4SH003739", "nickname": "Multivan T7", "model": "Multivan"},
            {"vin": "WVGZZZE27SE017858", "nickname": "ID.4", "model": "ID.4 Pure"},
        ]),
    }
    base.update(overrides)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_from_env_reads_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = Settings.from_env()
    assert settings.paperless_url == "https://paperless.example.test"
    assert settings.vehicles == {"WV2ZZZST4SH003739": "multivan", "WVGZZZE27SE017858": "id4"}
    assert settings.poll_interval_seconds == 900


def test_vehicles_parsing_skips_unrelated_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, MYGARAGE_VEHICLES=json.dumps([
        {"vin": "WVGZZZE27SE017858", "nickname": "ID.4", "model": "ID.4 Pure"},
        {"vin": "UNRELATED000000001", "nickname": "Some Other Car", "model": "Golf"},
    ]))
    assert Settings.from_env().vehicles == {"WVGZZZE27SE017858": "id4"}


def test_from_env_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("PAPERLESS_TOKEN")
    with pytest.raises(RuntimeError, match="PAPERLESS_TOKEN"):
        Settings.from_env()


def test_poll_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, POLL_INTERVAL="5m")
    assert Settings.from_env().poll_interval_seconds == 300
