from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

COST_SINKS = ("local", "mygarage")


@dataclass(frozen=True)
class VehicleConfig:
    """One entry of the vehicle registry, upserted into
    vehicle_pipeline.vehicles at startup (db.upsert_vehicles)."""

    slug: str
    vin: str
    label: str
    make: str | None = None
    model: str | None = None
    fuel_type: str | None = None


@dataclass(frozen=True)
class Settings:
    database_url: str
    paperless_url: str
    paperless_token: str
    paperless_webhook_secret: str
    litellm_base_url: str
    litellm_api_key: str
    mygarage_url: str | None
    mygarage_username: str | None
    mygarage_password: str | None
    vehicles: dict[str, str]
    ingestbuddy_handoff_secret: str
    poll_interval_seconds: int = 900
    cost_sink: str = "local"
    vehicle_configs: tuple[VehicleConfig, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> Settings:
        cost_sink = os.environ.get("COST_SINK", "local").strip().lower() or "local"
        if cost_sink not in COST_SINKS:
            raise RuntimeError(f"COST_SINK must be one of {', '.join(COST_SINKS)}, got {cost_sink!r}")

        # MyGarage credentials are only needed when it is still the sink.
        mygarage = _require if cost_sink == "mygarage" else _optional
        mygarage_url = mygarage("MYGARAGE_URL")

        raw_vehicles = os.environ.get("VEHICLES") or os.environ.get("MYGARAGE_VEHICLES")
        if not raw_vehicles:
            raise RuntimeError("missing required environment variable VEHICLES (or legacy MYGARAGE_VEHICLES)")
        vehicle_configs = parse_vehicle_configs(raw_vehicles)

        return cls(
            database_url=_require("DATABASE_URL"),
            paperless_url=_require("PAPERLESS_URL").rstrip("/"),
            paperless_token=_require("PAPERLESS_TOKEN"),
            paperless_webhook_secret=_require("PAPERLESS_WEBHOOK_SECRET"),
            litellm_base_url=_require("LITELLM_BASE_URL").rstrip("/"),
            litellm_api_key=_require("LITELLM_API_KEY"),
            mygarage_url=mygarage_url.rstrip("/") if mygarage_url else None,
            mygarage_username=mygarage("MYGARAGE_USERNAME"),
            mygarage_password=mygarage("MYGARAGE_PASSWORD"),
            vehicles={v.vin: v.slug for v in vehicle_configs},
            ingestbuddy_handoff_secret=_require("INGESTBUDDY_HANDOFF_SECRET"),
            poll_interval_seconds=_parse_interval(os.environ.get("POLL_INTERVAL", "15m")),
            cost_sink=cost_sink,
            vehicle_configs=vehicle_configs,
        )


def parse_vehicle_configs(raw: str) -> tuple[VehicleConfig, ...]:
    """Parse the vehicle registry from VEHICLES (or the legacy
    MYGARAGE_VEHICLES secret).

    The live value is a list of rich vehicle objects shared with the
    WiCAN/trip-enricher telemetry pipeline (vin, nickname, year, make,
    model, device_id, ...) -- confirmed 2026-09-12. Every entry with a VIN
    becomes a vehicle; unknown keys are ignored.

    Slug resolution, in order: an explicit ``slug`` key; the legacy
    model/nickname heuristic (ID.4 -> ``id4``, Multivan -> ``multivan``) so
    classify_vehicle.py's Paperless tag mapping keeps working against the
    existing secret unchanged; otherwise a slugified nickname/model, then
    the lower-cased VIN. Duplicate slugs get a numeric suffix.
    """
    entries = json.loads(raw)
    if isinstance(entries, dict):
        # Tolerate a plain {vin: label} map as well.
        entries = [{"vin": vin, "slug": label, "nickname": label} for vin, label in entries.items()]

    configs: list[VehicleConfig] = []
    seen: set[str] = set()
    for entry in entries:
        vin = str(entry.get("vin") or "").strip()
        if not vin:
            continue
        slug = _derive_slug(entry, vin)
        base, n = slug, 2
        while slug in seen:
            slug = f"{base}-{n}"
            n += 1
        seen.add(slug)
        model = _str_or_none(entry.get("model"))
        label = _str_or_none(entry.get("label")) or _str_or_none(entry.get("nickname")) or model or slug
        configs.append(
            VehicleConfig(
                slug=slug,
                vin=vin,
                label=label,
                make=_str_or_none(entry.get("make")),
                model=model,
                fuel_type=_str_or_none(entry.get("fuel_type")),
            )
        )
    return tuple(configs)


def _derive_slug(entry: dict, vin: str) -> str:
    explicit = _str_or_none(entry.get("slug"))
    if explicit:
        return _slugify(explicit) or vin.lower()
    text = f"{entry.get('model', '') or ''} {entry.get('nickname', '') or ''}".lower()
    if "id.4" in text or "id4" in text:
        return "id4"
    if "multivan" in text:
        return "multivan"
    for key in ("nickname", "model"):
        candidate = _slugify(_str_or_none(entry.get(key)) or "")
        if candidate:
            return candidate
    return vin.lower()


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name) or None


def _parse_interval(raw: str) -> int:
    raw = raw.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1]) * 60
    if raw.endswith("s"):
        return int(raw[:-1])
    return int(raw)
