"""Trip projection: raw drive spans + odometer readings + GPS breadcrumbs
-> trips. A pure function, recomputed over a trailing window every
detector cycle; the caller writes the returned upserts/removals/log.

Model (per vehicle):

* Spans of one person that overlap are merged first. Spans without a
  vehicle (Automotive-only, or CarPlay without a mapping) are candidates:
  resolved to the one vehicle whose odometer moved across the span, else to
  the one vehicle of an overlapping known-vehicle span, else dropped (a bus
  ride moves no odometer).
* Overlapping spans of different persons in the same vehicle form one
  cluster (driver + passengers); driver = highest span score.
* Consecutive clusters with no reading captured strictly between them form
  a group. lo = last reading captured <= group start; hi = the park
  snapshot -- the last reading captured within END_TOLERANCE before the
  group end, else the first reading captured >= group end. Readings
  captured elsewhere inside a cluster are internal and never mark parking.
* One cluster: start/end = lo/hi. Several: hi-lo is split proportionally to
  GPS distance (duration when any cluster lacks GPS), in 0.1 km units whose
  sum is exactly the delta (largest remainder).
* No hi yet -> odometer_pending (the feed lags hours); hi == lo while GPS
  saw > 1 km -> odometer_stale.
* Odometer increases between two consecutive readings not covered by any
  group become synthetic trips (source odometer_gap, driver unknown), so
  kilometres are never lost.

Trip keys are stable: ``span:<lowest member span id>`` and
``gap:<vehicle>:<lower reading id>``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from vehicle_pipeline.trips.models import (
    SOURCE_GAP,
    SOURCE_PHONE,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_SPLIT,
    STATUS_STALE,
    LogEntry,
    Position,
    ProjectionResult,
    Reading,
    Span,
    Trip,
)
from vehicle_pipeline.trips.signals import CLOSE_AFTER, span_score
from vehicle_pipeline.trips.util import haversine_km

# A reading this close before a group's end is its park snapshot (phone
# signals usually outlive ignition-off by a little).
END_TOLERANCE = CLOSE_AFTER
# Candidate (vehicle-less) spans shorter than this are Automotive flicker.
MIN_CANDIDATE = timedelta(minutes=2)
STALE_GPS_KM = Decimal("1")
TENTH = Decimal("0.1")


@dataclass
class _Span:
    id: int
    person: str
    vehicle_id: str | None
    start: datetime
    end: datetime | None
    evidence: dict[str, Any]
    member_ids: list[int] = field(default_factory=list)
    resolution: str | None = None

    def end_or(self, now: datetime) -> datetime:
        return self.end if self.end is not None else now


@dataclass
class _Cluster:
    vehicle_id: str
    spans: list[_Span]
    start: datetime
    end: datetime | None

    @property
    def anchor(self) -> int:
        return min(s.id for s in self.spans)


def project(
    *,
    vehicles: Sequence[str],
    spans: Sequence[Span],
    readings: Sequence[Reading],
    positions: Sequence[Position],
    existing: Mapping[str, Trip],
    now: datetime,
    window_start: datetime,
) -> ProjectionResult:
    stats: dict[str, int] = defaultdict(int)
    by_vehicle = _clean_readings(readings, vehicles)
    merged = _merge_person_spans(spans, now)
    resolved = _resolve_candidates(merged, by_vehicle, now, stats)
    pos_by_person: dict[str, list[Position]] = defaultdict(list)
    for p in sorted(positions, key=lambda p: p.ts):
        pos_by_person[p.person].append(p)

    desired: list[Trip] = []
    for vehicle in vehicles:
        vreadings = by_vehicle.get(vehicle, [])
        clusters = _clusters(vehicle, [s for s in resolved if s.vehicle_id == vehicle], now)
        groups = _groups(clusters, vreadings)
        covered: set[int] = set()
        for group in groups:
            trips, cov = _project_group(vehicle, group, vreadings, pos_by_person, now)
            covered |= cov
            desired.extend(t for t in trips if t.started_at >= window_start)
        desired.extend(t for t in _gap_trips(vehicle, vreadings, covered) if t.started_at >= window_start)

    desired.sort(key=lambda t: (t.started_at, t.trip_key))
    upserts, removals, log = _diff(desired, existing)
    for t in desired:
        stats[f"status:{t.status}"] += 1
    return ProjectionResult(trips=desired, upserts=upserts, removals=removals, log=log, stats=dict(stats))


# ---------------------------------------------------------------- readings


def _clean_readings(readings: Sequence[Reading], vehicles: Sequence[str]) -> dict[str, list[Reading]]:
    """Per vehicle, sorted by capture time; one reading per capture time
    (highest km); readings below the running maximum (a glitch on a
    total_increasing counter) dropped."""
    grouped: dict[str, dict[datetime, Reading]] = defaultdict(dict)
    for r in readings:
        cur = grouped[r.vehicle_id].get(r.captured_at)
        if cur is None or (r.km, -r.id) > (cur.km, -cur.id):
            grouped[r.vehicle_id][r.captured_at] = r
    out: dict[str, list[Reading]] = {}
    for vehicle in vehicles:
        clean: list[Reading] = []
        for r in sorted(grouped.get(vehicle, {}).values(), key=lambda r: r.captured_at):
            if clean and r.km < clean[-1].km:
                continue
            clean.append(r)
        out[vehicle] = clean
    return out


def _lo_index(readings: list[Reading], start: datetime) -> int | None:
    idx = None
    for i, r in enumerate(readings):
        if r.captured_at <= start:
            idx = i
        else:
            break
    return idx


def _hi_index(readings: list[Reading], last_start: datetime, end: datetime | None) -> int | None:
    """Park snapshot for a span/group ending at ``end`` whose last cluster
    started at ``last_start``."""
    if end is None:
        return None
    tolerance_idx = None
    for i, r in enumerate(readings):
        if last_start < r.captured_at < end and r.captured_at >= end - END_TOLERANCE:
            tolerance_idx = i
        if r.captured_at >= end:
            return tolerance_idx if tolerance_idx is not None else i
    return tolerance_idx


# ------------------------------------------------------------------- spans


def _merge_person_spans(spans: Sequence[Span], now: datetime) -> list[_Span]:
    by_person: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        by_person[s.person].append(s)
    out: list[_Span] = []
    for person in sorted(by_person):
        cur: _Span | None = None
        for s in sorted(by_person[person], key=lambda s: (s.started_at, s.id)):
            if cur is not None and s.started_at <= cur.end_or(now):
                cur.member_ids.append(s.id)
                cur.id = min(cur.id, s.id)
                if cur.end is not None:
                    cur.end = None if s.ended_at is None else max(cur.end, s.ended_at)
                cur.vehicle_id = cur.vehicle_id or s.vehicle_id
                cur.evidence = _merge_evidence(cur.evidence, s.evidence)
                continue
            if cur is not None:
                out.append(cur)
            cur = _Span(s.id, s.person, s.vehicle_id, s.started_at, s.ended_at, dict(s.evidence), [s.id])
        if cur is not None:
            out.append(cur)
    return out


def _merge_evidence(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    scores = dict(a.get("scores") or {})
    for k, v in (b.get("scores") or {}).items():
        scores[k] = max(int(v), int(scores.get(k, 0)))
    return {
        **a,
        "signals": sorted(set(a.get("signals") or []) | set(b.get("signals") or [])),
        "scores": scores,
        "base_score": max(int(a.get("base_score") or 0), int(b.get("base_score") or 0)),
    }


def _moved(readings: list[Reading], start: datetime, end: datetime | None) -> bool | None:
    """True/False whether the odometer moved across [start, end]; None when
    it cannot be known yet (no reading on one side)."""
    lo = _lo_index(readings, start)
    hi = _hi_index(readings, start, end)
    if lo is None or hi is None:
        return None
    return readings[hi].km > readings[lo].km


def _resolve_candidates(
    spans: list[_Span], by_vehicle: dict[str, list[Reading]], now: datetime, stats: dict[str, int]
) -> list[_Span]:
    known = [s for s in spans if s.vehicle_id is not None]
    out = list(known)
    for s in spans:
        if s.vehicle_id is not None:
            continue
        if s.end is not None and s.end - s.start < MIN_CANDIDATE:
            stats["candidates_too_short"] += 1
            continue
        moved = [v for v, rs in by_vehicle.items() if _moved(rs, s.start, s.end)]
        if len(moved) == 1:
            s.vehicle_id, s.resolution = moved[0], "odometer"
        else:
            overlapping = {
                k.vehicle_id
                for k in known
                if k.person != s.person and k.start < s.end_or(now) and s.start < k.end_or(now)
            }
            if len(overlapping) == 1:
                s.vehicle_id, s.resolution = overlapping.pop(), "overlap"
        if s.vehicle_id is None:
            stats["candidates_unresolved"] += 1
            continue
        stats[f"candidates_resolved_{s.resolution}"] += 1
        out.append(s)
    return out


def _clusters(vehicle: str, spans: list[_Span], now: datetime) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for s in sorted(spans, key=lambda s: (s.start, s.id)):
        last = clusters[-1] if clusters else None
        if last is not None and s.start <= (last.end if last.end is not None else now):
            last.spans.append(s)
            if last.end is not None:
                last.end = None if s.end is None else max(last.end, s.end)
            continue
        clusters.append(_Cluster(vehicle, [s], s.start, s.end))
    return clusters


def _groups(clusters: list[_Cluster], readings: list[Reading]) -> list[list[_Cluster]]:
    groups: list[list[_Cluster]] = []
    for c in clusters:
        if groups:
            prev = groups[-1][-1]
            between = prev.end is not None and any(prev.end < r.captured_at < c.start for r in readings)
            if not between:
                groups[-1].append(c)
                continue
        groups.append([c])
    return groups


# --------------------------------------------------------------- per group


def _driver(cluster: _Cluster, vehicle: str) -> tuple[str | None, dict[str, int]]:
    scores: dict[str, int] = {}
    first_seen: dict[str, datetime] = {}
    for s in cluster.spans:
        scores[s.person] = max(scores.get(s.person, 0), span_score(s.evidence, vehicle))
        first_seen[s.person] = min(first_seen.get(s.person, s.start), s.start)
    if not scores:
        return None, scores
    driver = sorted(scores, key=lambda p: (-scores[p], first_seen[p], p))[0]
    return driver, scores


def _gps_km(cluster: _Cluster, driver: str | None, pos: dict[str, list[Position]], now: datetime) -> Decimal | None:
    end = cluster.end if cluster.end is not None else now
    people = [driver] if driver else []
    people += sorted({s.person for s in cluster.spans} - set(people))
    for person in people:
        pts = [p for p in pos.get(person, []) if cluster.start <= p.ts <= end]
        if len(pts) >= 2:
            return Decimal(str(round(haversine_km(pts), 1)))
    return None


def _project_group(
    vehicle: str,
    group: list[_Cluster],
    readings: list[Reading],
    pos: dict[str, list[Position]],
    now: datetime,
) -> tuple[list[Trip], set[int]]:
    g_start, g_end = group[0].start, group[-1].end
    lo = _lo_index(readings, g_start)
    hi = _hi_index(readings, group[-1].start, g_end)
    first = lo if lo is not None else 0
    last = hi if hi is not None else len(readings) - 1
    covered = set(range(first, last))  # pair (i, i+1) for i in covered

    info = []
    for c in group:
        driver, scores = _driver(c, vehicle)
        info.append((c, driver, scores, _gps_km(c, driver, pos, now)))

    lo_km = readings[lo].km if lo is not None else None
    hi_km = readings[hi].km if hi is not None else None
    base_ev = {
        "group_size": len(group),
        "lo_reading_id": readings[lo].id if lo is not None else None,
        "hi_reading_id": readings[hi].id if hi is not None else None,
    }

    trips: list[Trip] = []
    if lo_km is None or hi_km is None:
        for i, (c, driver, scores, gps) in enumerate(info):
            trips.append(
                _trip(c, driver, scores, gps, lo_km if i == 0 else None, None, None, STATUS_PENDING, base_ev)
            )
        return trips, covered

    delta = hi_km - lo_km
    if len(group) == 1:
        c, driver, scores, gps = info[0]
        status = STATUS_STALE if delta == 0 and gps is not None and gps > STALE_GPS_KM else STATUS_OK
        trips.append(_trip(c, driver, scores, gps, lo_km, hi_km, delta, status, base_ev))
        return trips, covered

    use_gps = all(gps is not None and gps > 0 for _, _, _, gps in info)
    weights = (
        [float(gps) for _, _, _, gps in info]  # type: ignore[arg-type]
        if use_gps
        else [max(((c.end or now) - c.start).total_seconds(), 0.0) for c, _, _, _ in info]
    )
    shares = split_tenths(int((delta / TENTH).to_integral_value(ROUND_HALF_UP)), weights)
    ev = {**base_ev, "split_basis": "gps" if use_gps else "duration", "group_delta_km": str(delta)}
    odo = lo_km
    for (c, driver, scores, gps), share in zip(info, shares, strict=True):
        km = Decimal(share) * TENTH
        status = STATUS_STALE if delta == 0 and gps is not None and gps > STALE_GPS_KM else STATUS_SPLIT
        trips.append(_trip(c, driver, scores, gps, odo, odo + km, km, status, ev))
        odo = odo + km
    return trips, covered


def split_tenths(total: int, weights: Sequence[float]) -> list[int]:
    """Split an integer total proportionally to weights (largest remainder):
    non-negative parts whose sum is exactly total. Equal split when all
    weights are zero."""
    n = len(weights)
    if n == 0:
        return []
    w = [max(x, 0.0) for x in weights]
    s = sum(w)
    if s <= 0:
        w, s = [1.0] * n, float(n)
    raw = [total * x / s for x in w]
    parts = [int(r) for r in raw]  # total >= 0, so int() floors
    remainder = total - sum(parts)
    order = sorted(range(n), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:remainder]:
        parts[i] += 1
    return parts


def _trip(
    c: _Cluster,
    driver: str | None,
    scores: dict[str, int],
    gps: Decimal | None,
    odo_start: Decimal | None,
    odo_end: Decimal | None,
    km: Decimal | None,
    status: str,
    base_ev: dict[str, Any],
) -> Trip:
    return Trip(
        trip_key=f"span:{c.anchor}",
        vehicle_id=c.vehicle_id,
        driver=driver,
        started_at=c.start,
        ended_at=c.end,
        odo_start=odo_start,
        odo_end=odo_end,
        km=km.quantize(TENTH) if km is not None else None,
        gps_km=gps,
        status=status,
        source=SOURCE_PHONE,
        evidence={
            **base_ev,
            "span_ids": sorted(i for s in c.spans for i in s.member_ids),
            "scores": scores,
            "resolution": sorted({s.resolution for s in c.spans if s.resolution}),
        },
    )


def _gap_trips(vehicle: str, readings: list[Reading], covered: set[int]) -> list[Trip]:
    trips = []
    for i in range(len(readings) - 1):
        a, b = readings[i], readings[i + 1]
        if i in covered or b.km <= a.km:
            continue
        trips.append(
            Trip(
                trip_key=f"gap:{vehicle}:{a.id}",
                vehicle_id=vehicle,
                driver=None,
                started_at=a.captured_at,
                ended_at=b.captured_at,
                odo_start=a.km,
                odo_end=b.km,
                km=(b.km - a.km).quantize(TENTH),
                gps_km=None,
                status=STATUS_OK,
                source=SOURCE_GAP,
                evidence={"lo_reading_id": a.id, "hi_reading_id": b.id},
            )
        )
    return trips


# -------------------------------------------------------------------- diff


def _diff(desired: list[Trip], existing: Mapping[str, Trip]) -> tuple[list[Trip], list[str], list[LogEntry]]:
    upserts: list[Trip] = []
    log: list[LogEntry] = []
    seen: set[str] = set()
    for t in desired:
        seen.add(t.trip_key)
        old = existing.get(t.trip_key)
        if old is None:
            upserts.append(t)
            log.append(LogEntry(t.trip_key, None, t.as_json(), "created"))
            continue
        changed = [f for f in Trip.CORE_FIELDS if getattr(old, f) != getattr(t, f)]
        if not changed:
            continue
        upserts.append(t)
        if old.status == STATUS_PENDING and t.status != STATUS_PENDING:
            reason = "odometer_filled"
        else:
            reason = "changed:" + ",".join(changed)
        log.append(LogEntry(t.trip_key, old.as_json(), t.as_json(), reason))
    removals = sorted(k for k in existing if k not in seen)
    log.extend(LogEntry(k, existing[k].as_json(), None, "removed") for k in removals)
    return upserts, removals, log
