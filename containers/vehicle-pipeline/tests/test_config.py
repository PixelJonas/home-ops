import json

import pytest

from vehicle_pipeline.config import Settings, parse_vehicle_configs


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
        "INGESTBUDDY_HANDOFF_SECRET": "handoffsecret",
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


def test_vehicles_parsing_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every entry with a VIN becomes a vehicle (local cost sink registry);
    the legacy id4/multivan slugs are kept for classify_vehicle's tag map."""
    _env(monkeypatch, MYGARAGE_VEHICLES=json.dumps([
        {"vin": "TESTVIN0000000003", "nickname": "ID.4", "model": "ID.4 Pure", "make": "VW"},
        {"vin": "UNRELATED000000001", "nickname": "Some Other Car", "model": "Golf"},
        {"nickname": "no vin, skipped"},
    ]))
    settings = Settings.from_env()
    assert settings.vehicles == {"TESTVIN0000000003": "id4", "UNRELATED000000001": "some-other-car"}
    id4, other = settings.vehicle_configs
    assert (id4.slug, id4.label, id4.make, id4.model) == ("id4", "ID.4", "VW", "ID.4 Pure")
    assert (other.slug, other.label) == ("some-other-car", "Some Other Car")


def test_vehicles_env_wins_over_legacy_and_explicit_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, VEHICLES=json.dumps([
        {"vin": "TESTVIN0000000001", "slug": "Bulli", "label": "Der Bus", "fuel_type": "diesel"},
    ]))
    (v,) = Settings.from_env().vehicle_configs
    assert (v.slug, v.vin, v.label, v.fuel_type) == ("bulli", "TESTVIN0000000001", "Der Bus", "diesel")


def test_duplicate_slugs_get_suffix() -> None:
    configs = parse_vehicle_configs(json.dumps([
        {"vin": "TESTVIN0000000001", "model": "Multivan"},
        {"vin": "TESTVIN0000000002", "model": "Multivan"},
    ]))
    assert [c.slug for c in configs] == ["multivan", "multivan-2"]


def test_missing_vehicles_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("MYGARAGE_VEHICLES")
    with pytest.raises(RuntimeError, match="VEHICLES"):
        Settings.from_env()


def test_cost_sink_defaults_to_local_and_mygarage_creds_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    for key in ("MYGARAGE_URL", "MYGARAGE_USERNAME", "MYGARAGE_PASSWORD"):
        monkeypatch.delenv(key)
    settings = Settings.from_env()
    assert settings.cost_sink == "local"
    assert settings.mygarage_url is None


def test_cost_sink_mygarage_requires_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, COST_SINK="mygarage")
    assert Settings.from_env().cost_sink == "mygarage"
    monkeypatch.delenv("MYGARAGE_PASSWORD")
    with pytest.raises(RuntimeError, match="MYGARAGE_PASSWORD"):
        Settings.from_env()


def test_cost_sink_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, COST_SINK="excel")
    with pytest.raises(RuntimeError, match="COST_SINK"):
        Settings.from_env()


def test_from_env_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("PAPERLESS_TOKEN")
    with pytest.raises(RuntimeError, match="PAPERLESS_TOKEN"):
        Settings.from_env()


def test_poll_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, POLL_INTERVAL="5m")
    assert Settings.from_env().poll_interval_seconds == 300
