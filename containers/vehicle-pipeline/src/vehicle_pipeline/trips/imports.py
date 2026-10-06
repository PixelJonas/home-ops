"""Projection of imported in-car trip memories (VW app export, see
trips.vw_export) into trips: a pure function over ALL imported rows,
recomputed every detector cycle (they are few), so imported trips older than
the detector's trailing window stay in ``trips.trips``.

Model (per vehicle):

* Each export E holds the short-term trips the car remembered at
  E.created_at plus E's odometer at that moment (the anchor). Later exports
  repeat older rows; a row is stored once (row hash) and linked to every
  export that contained it (trips.imported_trip_exports).
* A row is *rewritten* -- dropped -- when a newer export covers its time
  ([first trip end of that export, its creation]) but no longer contains it:
  VW merged it with a later leg (stops < 2 h are merged), so the newer,
  longer row replaces it.
* Odometers are chained backwards inside one export: odo_end of E's last
  trip = E's anchor, odo_start = odo_end - km, the previous trip ends at
  that odo_start, and so on. A trip takes its odometer from the newest
  export containing it. Per-trip km are whole km as exported, so derived
  odometers can drift by a few km over many trips (noted in evidence).
* The anchor is trusted (status ``ok``) unless: E has no odometer; a trip
  of a newer export ended between E's last trip and E's creation (a trip
  was still open at export time); or the first odometer reading captured
  at/after E's last trip end, with no other trip/span in between, differs
  from the anchor. Those trips are ``odometer_pending`` (km still set) with
  the derived odometers in evidence.
* started_at = ended_at - driving time: an estimate (driving time excludes
  merged stops), flagged ``start_estimated``.
* Coverage: per export, [earliest estimated start, last trip end]. Detector
  trips (ha_phone / odometer_gap) of that vehicle fully inside a coverage
  interval (+- END_SLACK) are superseded by the import (trips.projection).

Trip keys: ``vw:<vehicle>:<ended_at, ISO UTC>``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from vehicle_pipeline.trips.models import STATUS_OK, STATUS_PENDING, Reading, Trip
from vehicle_pipeline.trips.signals import CLOSE_AFTER
from vehicle_pipeline.trips.vw_export import KIND_REFUEL, KIND_SHORT_TERM, SOURCE_VW_EXPORT

SOURCE_VW = SOURCE_VW_EXPORT
# Phone signals outlive ignition-off (and may precede the in-car driving
# clock) by about this much; detector trips within it of a coverage edge
# still count as inside.
END_SLACK = CLOSE_AFTER
TENTH = Decimal("0.1")
ROUNDING_NOTE = "per-trip km are whole km as exported; derived odometers can drift by a few km over many trips"


@dataclass(frozen=True)
class ImportExport:
    id: int
    source: str
    vehicle_id: str
    created_at: datetime
    odometer_km: Decimal | None


@dataclass(frozen=True)
class ImportedRow:
    id: int
    source: str
    vehicle_id: str
    kind: str
    ended_at: datetime
    km: Decimal
    driving_time: timedelta | None
    export_ids: tuple[int, ...]


@dataclass(frozen=True)
class AnchorCheck:
    """First odometer reading captured at/after an export's last trip end,
    and whether a detector span of the vehicle started in between."""

    reading: Reading | None
    span_between: bool = False


@dataclass
class ImportInputs:
    exports: Sequence[ImportExport] = ()
    rows: Sequence[ImportedRow] = ()  # short_term rows (others are ignored)
    checks: Mapping[int, AnchorCheck] = field(default_factory=dict)


@dataclass
class ImportProjection:
    trips: list[Trip]
    coverage: dict[str, list[tuple[datetime, datetime]]]
    stats: dict[str, int]


def _start(r: ImportedRow) -> datetime:
    return r.ended_at - (r.driving_time or timedelta(0))


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def trip_key(vehicle_id: str, ended_at: datetime) -> str:
    return f"vw:{vehicle_id}:{_iso(ended_at)}"


def _members(exports: Sequence[ImportExport], rows: Sequence[ImportedRow], kind: str) -> dict[int, list[ImportedRow]]:
    ids = {e.id for e in exports}
    out: dict[int, list[ImportedRow]] = {e.id: [] for e in exports}
    for r in rows:
        if r.kind != kind:
            continue
        for eid in r.export_ids:
            if eid in ids:
                out[eid].append(r)
    for lst in out.values():
        lst.sort(key=lambda r: (r.ended_at, r.id))
    return out


def anchor_points(exports: Sequence[ImportExport], rows: Sequence[ImportedRow]) -> dict[int, tuple[str, datetime]]:
    """export id -> (vehicle, last short-term trip end): where the store
    looks up each export's AnchorCheck."""
    members = _members(exports, rows, KIND_SHORT_TERM)
    by_id = {e.id: e for e in exports}
    return {eid: (by_id[eid].vehicle_id, m[-1].ended_at) for eid, m in members.items() if m}


def earliest_start(inputs: ImportInputs) -> datetime | None:
    """Lower bound of every coverage interval: stored trips from here on
    may be superseded, so the projection must see them."""
    starts = [_start(r) for r in inputs.rows if r.kind == KIND_SHORT_TERM]
    return min(starts) - END_SLACK if starts else None


def _chain(anchor: Decimal | None, rows: list[ImportedRow]) -> dict[int, tuple[Decimal, Decimal]]:
    """row id -> (odo_start, odo_end), chained backwards from the anchor."""
    out: dict[int, tuple[Decimal, Decimal]] = {}
    if anchor is None:
        return out
    odo = anchor
    for r in reversed(rows):
        out[r.id] = (odo - r.km, odo)
        odo -= r.km
    return out


def _newest(r: ImportedRow, by_id: Mapping[int, ImportExport], rank: Mapping[int, int]) -> ImportExport:
    """The newest export (of this vehicle) that contained the row."""
    return max((by_id[i] for i in r.export_ids if i in by_id), key=lambda e: rank[e.id])


def project_imports(inputs: ImportInputs) -> ImportProjection:
    stats: dict[str, int] = defaultdict(int)
    trips: list[Trip] = []
    coverage: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    exports_by_vehicle: dict[str, list[ImportExport]] = defaultdict(list)
    for e in inputs.exports:
        exports_by_vehicle[e.vehicle_id].append(e)

    for vehicle in sorted(exports_by_vehicle):
        exports = sorted(exports_by_vehicle[vehicle], key=lambda e: (e.created_at, e.id))
        rank = {e.id: i for i, e in enumerate(exports)}
        by_id = {e.id: e for e in exports}
        members = {k: v for k, v in _members(exports, inputs.rows, KIND_SHORT_TERM).items() if v}
        rows = {r.id: r for m in members.values() for r in m}

        valid: list[ImportedRow] = []
        for r in sorted(rows.values(), key=lambda r: (r.ended_at, r.id)):
            own = _newest(r, by_id, rank)
            rewritten = any(
                rank[eid] > rank[own.id] and m[0].ended_at <= r.ended_at <= by_id[eid].created_at
                for eid, m in members.items()
            )
            if rewritten:
                stats["imported_rewritten"] += 1
                continue
            valid.append(r)

        anchors: dict[int, dict[str, Any]] = {}
        chains: dict[int, dict[int, tuple[Decimal, Decimal]]] = {}
        for eid, m in members.items():
            e = by_id[eid]
            last_end = m[-1].ended_at
            info: dict[str, Any] = {
                "export_id": eid,
                "export_created_at": _iso(e.created_at),
                "odometer_km": _s(e.odometer_km),
                "last_trip_end": _iso(last_end),
                "reading_km": None,
                "reading_captured_at": None,
            }
            check = inputs.checks.get(eid, AnchorCheck(None))
            if e.odometer_km is None:
                info["check"] = "no_anchor"
            elif any(last_end < r.ended_at <= e.created_at for r in valid):
                info["check"] = "trip_after_last"
            elif check.reading is None:
                info["check"] = "no_reading"
            else:
                rd = check.reading
                info["reading_km"] = _s(rd.km)
                info["reading_captured_at"] = _iso(rd.captured_at)
                if check.span_between or any(last_end < r.ended_at <= rd.captured_at for r in valid):
                    info["check"] = "unverifiable_trip_between"
                elif rd.km == e.odometer_km:
                    info["check"] = "verified"
                else:
                    info["check"] = "mismatch"
            info["trusted"] = info["check"] in ("verified", "no_reading", "unverifiable_trip_between")
            anchors[eid] = info
            chains[eid] = _chain(e.odometer_km, m)
            coverage[vehicle].append((min(_start(r) for r in m), last_end))

        seen: set[str] = set()
        for r in valid:
            own = _newest(r, by_id, rank)
            anchor = anchors[own.id]
            odo = chains[own.id].get(r.id)
            key = trip_key(vehicle, r.ended_at)
            if key in seen:  # two rows ending in the same minute: keep both
                key = f"{key}:{r.id}"
            seen.add(key)
            evidence: dict[str, Any] = {
                "imported_trip_id": r.id,
                "start_estimated": True,
                "driving_time_min": int((r.driving_time or timedelta(0)).total_seconds() // 60),
                "driving_time_capped": r.driving_time is not None and r.driving_time >= timedelta(hours=99, minutes=59),
                "exports": len(r.export_ids),
                "anchor": {k: v for k, v in anchor.items() if k != "trusted"},
                "odometer_derived": odo is not None,
                "odometer_rounding": ROUNDING_NOTE,
            }
            if anchor["trusted"] and odo is not None:
                status, odo_start, odo_end = STATUS_OK, odo[0], odo[1]
            else:
                status, odo_start, odo_end = STATUS_PENDING, None, None
                if odo is not None:
                    evidence["derived_odo_start"], evidence["derived_odo_end"] = _s(odo[0]), _s(odo[1])
            stats[f"imported_status:{status}"] += 1
            trips.append(
                Trip(
                    trip_key=key,
                    vehicle_id=vehicle,
                    driver=None,
                    started_at=_start(r),
                    ended_at=r.ended_at,
                    odo_start=odo_start,
                    odo_end=odo_end,
                    km=r.km.quantize(TENTH),
                    gps_km=None,
                    status=status,
                    source=SOURCE_VW,
                    evidence=evidence,
                )
            )
    trips.sort(key=lambda t: (t.started_at, t.trip_key))
    return ImportProjection(trips=trips, coverage={k: sorted(v) for k, v in coverage.items()}, stats=dict(stats))


def covered(intervals: Sequence[tuple[datetime, datetime]], trip: Trip) -> bool:
    """Trip lies fully inside one coverage interval (+- END_SLACK)."""
    if trip.ended_at is None:
        return False
    return any(s - END_SLACK <= trip.started_at and trip.ended_at <= e + END_SLACK for s, e in intervals)


def supersede_target(vw_trips: Sequence[Trip], trip: Trip) -> str:
    """The imported trip a superseded detector trip belongs to: the first
    one (by end) that ends no earlier than the detector trip (+ END_SLACK)
    and that the detector trip started before. Annotations on the detector
    trip are inherited through it (trips_effective)."""
    ordered = sorted(vw_trips, key=lambda t: (t.ended_at, t.trip_key))
    assert trip.ended_at is not None
    for v in ordered:
        assert v.ended_at is not None
        if v.ended_at + END_SLACK >= trip.ended_at and trip.started_at < v.ended_at:
            return v.trip_key
    return ordered[-1].trip_key


# ------------------------------------------------------------ refuel check


@dataclass(frozen=True)
class RefuelCheck:
    refuel_at: datetime
    segment_km: Decimal
    odometer_from_segments: Decimal | None
    trip_key: str | None
    trip_odo_start: Decimal | None
    trip_odo_end: Decimal | None
    position: str  # 'inside' (refuel during a merged trip) | 'between' | 'unknown'
    diff_km: Decimal | None  # 0 when the segment odometer lies within the trip bracket


def refuel_crosscheck(
    exports: Sequence[ImportExport], rows: Sequence[ImportedRow], trips: Sequence[Trip]
) -> list[RefuelCheck]:
    """Odometer at each completed refuel, two ways: chained backwards over
    the refuel segments from the export anchor (the last segment of an
    export is the open one since the last refuel and ends at the anchor),
    and from the imported trips (odo_end of the trip before, or the
    [odo_start, odo_end] bracket of the trip the refuel happened in --
    refuel stops are usually merged into a trip). The odometer feeds the
    fuel cost rows; diff_km shows chain/rounding drift. One vehicle per
    call; a refuel row takes its odometer from the newest export holding it."""
    members = {k: v for k, v in _members(exports, rows, KIND_REFUEL).items() if v}
    by_id = {e.id: e for e in exports}
    rank = {e.id: i for i, e in enumerate(sorted(exports, key=lambda e: (e.created_at, e.id)))}
    owner: dict[int, int] = {}
    odo_at: dict[int, Decimal | None] = {}
    for eid, m in members.items():
        anchor = by_id[eid].odometer_km
        odo = anchor
        for r in reversed(m):
            if r.id not in owner or rank[eid] > rank[owner[r.id]]:
                owner[r.id] = eid
                odo_at[r.id] = odo
            odo = None if odo is None else odo - r.km
    vw = sorted((t for t in trips if t.ended_at is not None), key=lambda t: t.ended_at)  # type: ignore[arg-type, return-value]
    out: list[RefuelCheck] = []
    seen_ids: set[int] = set()
    for m in members.values():
        for r in m:
            if r.id in seen_ids:
                continue
            seen_ids.add(r.id)
            if r.id == members[owner[r.id]][-1].id:
                continue  # the open segment: no refuel yet
            seg_odo = odo_at[r.id]
            inside = next((t for t in vw if t.started_at <= r.ended_at <= t.ended_at), None)  # type: ignore[operator]
            before = [t for t in vw if t.ended_at <= r.ended_at]  # type: ignore[operator]
            t = inside or (before[-1] if before else None)
            pos = "inside" if inside else ("between" if t else "unknown")
            diff: Decimal | None = None
            if t is not None and seg_odo is not None and t.odo_end is not None and t.odo_start is not None:
                lo, hi = (t.odo_start, t.odo_end) if inside else (t.odo_end, t.odo_end)
                diff = seg_odo - hi if seg_odo > hi else (seg_odo - lo if seg_odo < lo else Decimal(0))
            out.append(
                RefuelCheck(
                    refuel_at=r.ended_at,
                    segment_km=r.km,
                    odometer_from_segments=seg_odo,
                    trip_key=t.trip_key if t else None,
                    trip_odo_start=t.odo_start if t else None,
                    trip_odo_end=t.odo_end if t else None,
                    position=pos,
                    diff_km=diff,
                )
            )
    out.sort(key=lambda c: c.refuel_at)
    return out
