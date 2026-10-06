from __future__ import annotations

import json

import pytest

from vehicle_pipeline.trips.settings import (
    TripSettings,
    parse_phones,
    parse_trip_vehicles,
)

# Same shape as the live trip-enricher secret: {VIN: {name, hotspot_ssid}}.
ENRICHER_VEHICLES = json.dumps(
    {
        "VINFAKE00000000A1": {"name": "Our ID.4", "hotspot_ssid": "hotspot-a"},
        "VINFAKE00000000B2": {"name": "The Multivan", "hotspot_ssid": "hotspot-b"},
    }
)
ENRICHER_PHONES = json.dumps(
    {
        "alice": {
            "tracker": "device_tracker.alice_phone",
            "ssid": "sensor.alice_phone_ssid",
            "audio": "sensor.alice_phone_audio_output",
            "activity": "sensor.alice_phone_activity",
        },
        "bob": {"device_tracker": "device_tracker.bob_phone", "activity_sensor": "sensor.bob_activity", "carplay_vehicle": "id4"},
    }
)


def test_slugs_from_name_heuristic_match_vehicle_registry_ids() -> None:
    vehicles = parse_trip_vehicles(ENRICHER_VEHICLES)
    assert [(v.slug, v.vin, v.hotspot_ssid) for v in vehicles] == [
        ("id4", "VINFAKE00000000A1", "hotspot-a"),
        ("multivan", "VINFAKE00000000B2", "hotspot-b"),
    ]
    assert all(v.odometer_entity is None for v in vehicles)


def test_registry_vin_mapping_and_odometer_overrides_win() -> None:
    vehicles = parse_trip_vehicles(
        ENRICHER_VEHICLES,
        vin_to_slug={"VINFAKE00000000A1": "electric"},
        odometer_overrides={"electric": "sensor.car_a_mileage", "multivan": "sensor.car_b_mileage"},
    )
    assert [(v.slug, v.odometer_entity) for v in vehicles] == [
        ("electric", "sensor.car_a_mileage"),
        ("multivan", "sensor.car_b_mileage"),
    ]


def test_list_shape_with_explicit_slug_and_odometer_entity() -> None:
    raw = json.dumps([{"vin": "X1", "slug": "van", "label": "Van", "ssid": "s", "odometer_entity": "sensor.van_odo"}])
    (v,) = parse_trip_vehicles(raw)
    assert (v.slug, v.name, v.hotspot_ssid, v.odometer_entity) == ("van", "Van", "s", "sensor.van_odo")


def test_phones_accept_aliases_and_carplay_vehicle() -> None:
    alice, bob = parse_phones(ENRICHER_PHONES)
    assert alice.signal_entities() == ["sensor.alice_phone_ssid", "sensor.alice_phone_audio_output", "sensor.alice_phone_activity"]
    assert (bob.tracker, bob.activity, bob.ssid, bob.carplay_vehicle) == (
        "device_tracker.bob_phone",
        "sensor.bob_activity",
        None,
        "id4",
    )


def test_from_env_needs_no_paperless_litellm_or_mygarage(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("PAPERLESS_URL", "LITELLM_API_KEY", "MYGARAGE_URL", "VEHICLES", "MYGARAGE_VEHICLES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("HA_URL", "http://ha.example.test/")
    monkeypatch.setenv("HA_TOKEN", "token")
    monkeypatch.setenv("TRIP_VEHICLES", ENRICHER_VEHICLES)
    monkeypatch.setenv("TRIP_PHONES", ENRICHER_PHONES)
    monkeypatch.setenv("TRIP_ODOMETER_ENTITIES", json.dumps({"id4": "sensor.car_a_mileage"}))
    s = TripSettings.from_env()
    assert s.ha_url == "http://ha.example.test"
    assert {v.slug: v.odometer_entity for v in s.vehicles} == {"id4": "sensor.car_a_mileage", "multivan": None}
    assert s.poll_interval_seconds == 60 and s.recompute_days == 45 and s.backfill_days == 30


def test_from_env_reports_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIP_PHONES", raising=False)
    monkeypatch.setenv("TRIP_VEHICLES", ENRICHER_VEHICLES)
    with pytest.raises(RuntimeError, match="TRIP_PHONES"):
        TripSettings.from_env()
