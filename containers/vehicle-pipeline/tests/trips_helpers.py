"""Builders shared by the trip detector tests. All entity IDs, persons and
SSIDs are fake."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from vehicle_pipeline.trips.models import Position, Reading, Span
from vehicle_pipeline.trips.settings import PhoneConfig, TripVehicle

BASE = datetime(2026, 9, 1, tzinfo=UTC)

VEHICLES = (
    TripVehicle(slug="id4", vin="VINFAKE00000000A1", name="Test ID.4", hotspot_ssid="hotspot-a", odometer_entity="sensor.car_a_mileage"),
    TripVehicle(slug="multivan", vin="VINFAKE00000000B2", name="Test Multivan", hotspot_ssid="hotspot-b", odometer_entity="sensor.car_b_mileage"),
)

ALICE = PhoneConfig(
    person="alice",
    tracker="device_tracker.alice_phone",
    ssid="sensor.alice_phone_ssid",
    audio="sensor.alice_phone_audio_output",
    activity="sensor.alice_phone_activity",
)
BOB = PhoneConfig(
    person="bob",
    tracker="device_tracker.bob_phone",
    ssid="sensor.bob_phone_ssid",
    audio="sensor.bob_phone_audio_output",
    activity="sensor.bob_phone_activity",
)


def t(hour: int, minute: int = 0, day: int = 0) -> datetime:
    return BASE + timedelta(days=day, hours=hour, minutes=minute)


def reading(rid: int, km: float | str, at: datetime, vehicle: str = "id4") -> Reading:
    return Reading(rid, vehicle, Decimal(str(km)), at)


def scores(vehicle: str | None, *, hotspot: bool = False, carplay: bool = False, automotive: bool = False) -> dict[str, Any]:
    base = (5 if carplay else 0) + (3 if automotive else 0)
    sc = {v.slug: base + (10 if hotspot and v.slug == vehicle else 0) for v in VEHICLES}
    signals = [k for k, on in (("hotspot_ssid", hotspot), ("carplay", carplay), ("automotive", automotive)) if on]
    return {"scores": sc, "base_score": base, "signals": signals}


def span(
    sid: int,
    start: datetime,
    end: datetime | None,
    vehicle: str | None = "id4",
    person: str = "alice",
    **sig: bool,
) -> Span:
    if not sig:
        sig = {"hotspot": vehicle is not None, "carplay": True}
    return Span(sid, person, vehicle, start, end, scores(vehicle, **sig))


def track(person: str, start: datetime, end: datetime, km: float, n: int = 20) -> list[Position]:
    """n points on a straight north-bound line covering ~km kilometres."""
    step_deg = (km / 111.195) / (n - 1)
    dt = (end - start) / (n - 1)
    return [Position(person, start + dt * i if i < n - 1 else end, 50.0 + step_deg * i, 8.0) for i in range(n)]


def state(entity_id: str, value: str, at: datetime, attrs: dict[str, Any] | None = None, updated: datetime | None = None) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": attrs or {},
        "last_changed": at.isoformat(),
        "last_updated": (updated or at).isoformat(),
    }
