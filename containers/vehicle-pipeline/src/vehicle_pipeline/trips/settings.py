"""TripSettings: the detector's own env contract. Deliberately independent
of vehicle_pipeline.config.Settings so the detector Deployment needs no
Paperless/LiteLLM/MyGarage variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from vehicle_pipeline.config import _derive_slug, parse_vehicle_configs


@dataclass(frozen=True)
class TripVehicle:
    slug: str  # must match vehicle_pipeline.vehicles.id
    vin: str | None
    name: str
    hotspot_ssid: str | None = None
    odometer_entity: str | None = None


@dataclass(frozen=True)
class PhoneConfig:
    person: str
    tracker: str | None = None
    ssid: str | None = None
    audio: str | None = None
    activity: str | None = None
    # Optional: vehicle slug this person's CarPlay always means (e.g. only
    # one car has CarPlay). Without it, a CarPlay-only span has no vehicle
    # and is resolved by the projection from the odometers.
    carplay_vehicle: str | None = None

    def entities(self) -> list[str]:
        return [e for e in (self.tracker, self.ssid, self.audio, self.activity) if e]

    def signal_entities(self) -> list[str]:
        return [e for e in (self.ssid, self.audio, self.activity) if e]


@dataclass(frozen=True)
class TripSettings:
    database_url: str
    ha_url: str
    ha_token: str
    vehicles: tuple[TripVehicle, ...]
    phones: tuple[PhoneConfig, ...]
    poll_interval_seconds: int = 60
    heartbeat_file: str = "/tmp/vehicle-pipeline-detector-heartbeat"
    recompute_days: int = 45
    backfill_days: int = 30

    @classmethod
    def from_env(cls) -> TripSettings:
        registry = os.environ.get("VEHICLES") or os.environ.get("MYGARAGE_VEHICLES")
        vin_to_slug = {v.vin: v.slug for v in parse_vehicle_configs(registry)} if registry else {}
        overrides = _parse_json_map(os.environ.get("TRIP_ODOMETER_ENTITIES"), "TRIP_ODOMETER_ENTITIES")
        vehicles = parse_trip_vehicles(_require("TRIP_VEHICLES"), vin_to_slug, overrides)
        phones = parse_phones(_require("TRIP_PHONES"))
        if not vehicles:
            raise RuntimeError("TRIP_VEHICLES contains no usable vehicle entries")
        return cls(
            database_url=_require("DATABASE_URL"),
            ha_url=_require("HA_URL").rstrip("/"),
            ha_token=_require("HA_TOKEN"),
            vehicles=vehicles,
            phones=phones,
            poll_interval_seconds=int(os.environ.get("TRIP_POLL_INTERVAL", "60")),
            heartbeat_file=os.environ.get("HEARTBEAT_FILE", "/tmp/vehicle-pipeline-detector-heartbeat"),
            recompute_days=int(os.environ.get("TRIP_RECOMPUTE_DAYS", "45")),
            backfill_days=int(os.environ.get("TRIP_BACKFILL_DAYS", "30")),
        )


def parse_trip_vehicles(
    raw: str,
    vin_to_slug: dict[str, str] | None = None,
    odometer_overrides: dict[str, str] | None = None,
) -> tuple[TripVehicle, ...]:
    """Parse the trip-enricher vehicle config (Doppler
    MYGARAGE_TRIP_ENRICHER_VEHICLES). Same shapes as
    trip_enricher.normalize_vehicles: a dict keyed by VIN or a list of
    objects; name/vehicle/label and hotspot_ssid/hotspotSsid/ssid aliases.

    Slug, in order: explicit ``slug``; the VIN looked up in the vehicle
    registry (VEHICLES / MYGARAGE_VEHICLES, same mapping the app upserts
    into vehicle_pipeline.vehicles); the registry's model/nickname heuristic
    applied to the name (ID.4 -> id4, Multivan -> multivan).

    Odometer entity: optional per-entry ``odometer_entity``, overridden by
    the TRIP_ODOMETER_ENTITIES {slug: entity_id} map.
    """
    vin_to_slug = vin_to_slug or {}
    odometer_overrides = odometer_overrides or {}
    data = json.loads(raw)
    if isinstance(data, dict):
        entries = list(data.items())
    elif isinstance(data, list):
        entries = [(None, item) for item in data]
    else:
        raise RuntimeError("TRIP_VEHICLES must be a JSON object or list")

    out: list[TripVehicle] = []
    seen: set[str] = set()
    for key, item in entries:
        if not isinstance(item, dict):
            continue
        vin = _pick(item, "vin", "VIN") or key
        vin = str(vin) if vin else None
        name = str(_pick(item, "name", "vehicle", "label") or vin or "")
        explicit = _pick(item, "slug")
        if explicit:
            slug = str(explicit)
        elif vin and vin in vin_to_slug:
            slug = vin_to_slug[vin]
        else:
            slug = _derive_slug({"nickname": name, "model": name}, (vin or name).lower())
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(
            TripVehicle(
                slug=slug,
                vin=vin,
                name=name or slug,
                hotspot_ssid=_pick(item, "hotspot_ssid", "hotspotSsid", "ssid"),
                odometer_entity=odometer_overrides.get(slug) or _pick(item, "odometer_entity", "odometer"),
            )
        )
    return tuple(out)


def parse_phones(raw: str) -> tuple[PhoneConfig, ...]:
    """Parse the trip-enricher phone config (Doppler
    MYGARAGE_TRIP_ENRICHER_PHONES); same shapes/aliases as
    trip_enricher.normalize_phones, plus optional ``carplay_vehicle``."""
    data = json.loads(raw)
    if isinstance(data, dict):
        entries = list(data.items())
    elif isinstance(data, list):
        entries = [(_pick(c, "name", "person"), c) for c in data if isinstance(c, dict)]
    else:
        raise RuntimeError("TRIP_PHONES must be a JSON object or list")
    phones: list[PhoneConfig] = []
    for name, cfg in entries:
        if not name or not isinstance(cfg, dict):
            continue
        phones.append(
            PhoneConfig(
                person=str(name),
                tracker=_pick(cfg, "tracker", "device_tracker"),
                ssid=_pick(cfg, "ssid", "ssid_sensor"),
                audio=_pick(cfg, "audio", "audio_sensor"),
                activity=_pick(cfg, "activity", "activity_sensor"),
                carplay_vehicle=_pick(cfg, "carplay_vehicle"),
            )
        )
    return tuple(phones)


def _pick(d: dict, *keys: str) -> str | None:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _parse_json_map(raw: str | None, name: str) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in data.items() if v}


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value
