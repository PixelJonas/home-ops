from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    paperless_url: str
    paperless_token: str
    paperless_webhook_secret: str
    litellm_base_url: str
    litellm_api_key: str
    mygarage_url: str
    mygarage_username: str
    mygarage_password: str
    vehicles: dict[str, str]
    poll_interval_seconds: int = 900

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_require("DATABASE_URL"),
            paperless_url=_require("PAPERLESS_URL").rstrip("/"),
            paperless_token=_require("PAPERLESS_TOKEN"),
            paperless_webhook_secret=_require("PAPERLESS_WEBHOOK_SECRET"),
            litellm_base_url=_require("LITELLM_BASE_URL").rstrip("/"),
            litellm_api_key=_require("LITELLM_API_KEY"),
            mygarage_url=_require("MYGARAGE_URL").rstrip("/"),
            mygarage_username=_require("MYGARAGE_USERNAME"),
            mygarage_password=_require("MYGARAGE_PASSWORD"),
            vehicles=_parse_vehicles(_require("MYGARAGE_VEHICLES")),
            poll_interval_seconds=_parse_interval(os.environ.get("POLL_INTERVAL", "15m")),
        )


def _parse_vehicles(raw: str) -> dict[str, str]:
    """MYGARAGE_VEHICLES is a list of rich vehicle objects shared with the
    WiCAN/trip-enricher telemetry pipeline (vin, nickname, year, make, model,
    device_id, ...) -- not a plain {vin: label} map. Confirmed against the
    live Doppler value 2026-09-12 (this Settings module originally assumed
    the wrong shape, silently caught only once a real document reached
    classify_vehicle() and crashed on vehicles.items()). Extract just what
    classify_vehicle() needs: vin -> "id4"/"multivan", matching
    classify_vehicle.py's _TAG_TO_LABEL values exactly. Entries for neither
    vehicle (e.g. a future third car added for trip-enricher only) are
    silently skipped, not an error -- this pipeline only cares about these
    two per ticket #16's scope."""
    vehicles: dict[str, str] = {}
    for entry in json.loads(raw):
        text = f"{entry.get('model', '')} {entry.get('nickname', '')}".lower()
        if "id.4" in text or "id4" in text:
            vehicles[entry["vin"]] = "id4"
        elif "multivan" in text:
            vehicles[entry["vin"]] = "multivan"
    return vehicles


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _parse_interval(raw: str) -> int:
    raw = raw.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1]) * 60
    if raw.endswith("s"):
        return int(raw[:-1])
    return int(raw)
