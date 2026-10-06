from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

DETECTOR_VERSION = 1

STATUS_OK = "ok"
STATUS_SPLIT = "odometer_split"
STATUS_PENDING = "odometer_pending"
STATUS_STALE = "odometer_stale"

SOURCE_PHONE = "ha_phone"
SOURCE_GAP = "odometer_gap"


@dataclass(frozen=True)
class ReadingIn:
    """An odometer reading as extracted from HA history (pre-insert)."""

    vehicle_id: str
    km: Decimal
    data_captured_at: datetime
    ha_last_changed: datetime | None
    source: str


@dataclass(frozen=True)
class Reading:
    id: int
    vehicle_id: str
    km: Decimal
    captured_at: datetime


@dataclass(frozen=True)
class DetectedSpan:
    """A drive span as detected from one person's phone signals."""

    person: str
    vehicle_id: str | None
    started_at: datetime
    ended_at: datetime | None  # None = still open
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Span:
    """A stored drive span (has a DB id)."""

    id: int
    person: str
    vehicle_id: str | None
    started_at: datetime
    ended_at: datetime | None
    evidence: dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_PHONE


@dataclass(frozen=True)
class Position:
    person: str
    ts: datetime
    lat: float
    lon: float
    speed_kmh: float | None = None


@dataclass(frozen=True)
class Trip:
    trip_key: str
    vehicle_id: str | None
    driver: str | None
    started_at: datetime
    ended_at: datetime | None
    odo_start: Decimal | None
    odo_end: Decimal | None
    km: Decimal | None
    gps_km: Decimal | None
    status: str
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)

    # Fields whose change counts as a real projection change (evidence is
    # bookkeeping and never triggers a log row on its own).
    CORE_FIELDS = (
        "vehicle_id",
        "driver",
        "started_at",
        "ended_at",
        "odo_start",
        "odo_end",
        "km",
        "gps_km",
        "status",
        "source",
    )

    def core(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.CORE_FIELDS}

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.core().items():
            if isinstance(v, datetime):
                out[k] = v.isoformat()
            elif isinstance(v, Decimal):
                out[k] = str(v)
            else:
                out[k] = v
        return out


@dataclass(frozen=True)
class LogEntry:
    trip_key: str
    old: dict[str, Any] | None
    new: dict[str, Any] | None
    reason: str


@dataclass
class ProjectionResult:
    trips: list[Trip]
    upserts: list[Trip]
    removals: list[str]
    log: list[LogEntry]
    stats: dict[str, int] = field(default_factory=dict)
