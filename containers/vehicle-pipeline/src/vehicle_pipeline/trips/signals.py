"""Raw-signal extraction from HA history: odometer readings, phone drive
spans, GPS breadcrumbs. Pure functions (no I/O)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from vehicle_pipeline.trips.models import DetectedSpan, Position, ReadingIn
from vehicle_pipeline.trips.settings import PhoneConfig, TripVehicle
from vehicle_pipeline.trips.util import parse_number, parse_ts

# A span closes after this long without any supporting signal; shorter
# gaps between supporting signals are merged into one span.
CLOSE_AFTER = timedelta(minutes=5)

CARPLAY = "CarPlay"
AUTOMOTIVE = "Automotive"

# score_person weights, ported from trip_enricher.score_person.
SCORE_HOTSPOT = 10
SCORE_CARPLAY = 5
SCORE_AUTOMOTIVE = 3

READING_SOURCE_CAPTURED = "ha_data_captured_at"
READING_SOURCE_LAST_CHANGED = "ha_last_changed"


def _state_value(entry: dict[str, Any]) -> str | None:
    # Ported from trip_enricher._state_value.
    v = entry.get("state")
    return None if v in (None, "unknown", "unavailable") else str(v)


# --------------------------------------------------------------- odometer


def extract_readings(vehicle_id: str, rows: Iterable[dict[str, Any]]) -> list[ReadingIn]:
    """Odometer history rows -> readings, deduped on (capture time, km).

    The reading time is the ``data_captured_at`` attribute (when the car
    took the snapshot), not ``last_changed`` (when the delayed feed
    delivered it); the feed re-reports the same capture every ~15 min while
    parked, which collapses here and again on the table's UNIQUE key. Rows
    without the attribute fall back to last_changed under a distinct
    source so the two are never confused.
    """
    out: dict[tuple[datetime, Decimal, str], ReadingIn] = {}
    for row in rows:
        raw = _state_value(row)
        if raw is None:
            continue
        try:
            km = Decimal(raw)
        except InvalidOperation:
            continue
        if not km.is_finite():
            continue
        last_changed = parse_ts(row.get("last_changed") or row.get("last_updated"))
        captured = parse_ts((row.get("attributes") or {}).get("data_captured_at"))
        source = READING_SOURCE_CAPTURED
        if captured is None:
            captured, source = last_changed, READING_SOURCE_LAST_CHANGED
        if captured is None:
            continue
        key = (captured, km, source)
        if key not in out:
            out[key] = ReadingIn(vehicle_id, km, captured, last_changed, source)
    return sorted(out.values(), key=lambda r: (r.data_captured_at, r.km))


# ------------------------------------------------------------------ spans


@dataclass(frozen=True)
class _Interval:
    start: datetime
    end: datetime
    kind: str  # hotspot_ssid | carplay | automotive
    vehicle_id: str | None


def state_intervals(rows: Sequence[dict[str, Any]], window_end: datetime) -> list[tuple[datetime, datetime, str | None]]:
    """History rows -> [(start, end, value)], each state valid until the
    next row's last_changed; the last one until window_end."""
    timed = []
    for r in rows:
        ts = parse_ts(r.get("last_changed") or r.get("last_updated"))
        if ts is not None:
            timed.append((ts, _state_value(r)))
    timed.sort(key=lambda t: t[0])
    out = []
    for i, (ts, value) in enumerate(timed):
        end = timed[i + 1][0] if i + 1 < len(timed) else window_end
        if end > ts:
            out.append((ts, end, value))
    return out


def score_person(cfg: PhoneConfig, history: dict[str, list[dict[str, Any]]], hotspot_ssid: str | None) -> tuple[int, list[str]]:
    """Ported from trip_enricher.score_person: hotspot SSID == the
    vehicle's +10, audio output CarPlay +5, activity Automotive +3."""
    score = 0
    signals: list[str] = []
    if hotspot_ssid:
        for e in history.get(cfg.ssid or "", []):
            if _state_value(e) == str(hotspot_ssid):
                score += SCORE_HOTSPOT
                signals.append("hotspot_ssid")
                break
    for e in history.get(cfg.audio or "", []):
        if _state_value(e) == CARPLAY:
            score += SCORE_CARPLAY
            signals.append("carplay")
            break
    for e in history.get(cfg.activity or "", []):
        if _state_value(e) == AUTOMOTIVE:
            score += SCORE_AUTOMOTIVE
            signals.append("automotive")
            break
    return score, signals


def detect_spans(
    phone: PhoneConfig,
    history: dict[str, list[dict[str, Any]]],
    vehicles: Sequence[TripVehicle],
    window_end: datetime,
) -> list[DetectedSpan]:
    """One person's phone history -> drive spans.

    Supporting signals: Wi-Fi SSID equal to a vehicle's hotspot (vehicle
    known), audio output CarPlay (vehicle = the person's carplay_vehicle if
    configured, else unknown), activity Automotive (vehicle unknown; a
    candidate the projection resolves from the odometers). Signals closer
    than CLOSE_AFTER merge; a span whose last signal is within CLOSE_AFTER
    of window_end stays open (ended_at None).
    """
    by_ssid = {v.hotspot_ssid: v.slug for v in vehicles if v.hotspot_ssid}
    intervals: list[_Interval] = []
    if phone.ssid:
        for s, e, val in state_intervals(history.get(phone.ssid, []), window_end):
            if val is not None and val in by_ssid:
                intervals.append(_Interval(s, e, "hotspot_ssid", by_ssid[val]))
    if phone.audio:
        for s, e, val in state_intervals(history.get(phone.audio, []), window_end):
            if val == CARPLAY:
                intervals.append(_Interval(s, e, "carplay", phone.carplay_vehicle))
    if phone.activity:
        for s, e, val in state_intervals(history.get(phone.activity, []), window_end):
            if val == AUTOMOTIVE:
                intervals.append(_Interval(s, e, "automotive", None))
    if not intervals:
        return []

    intervals.sort(key=lambda i: i.start)
    groups: list[list[_Interval]] = [[intervals[0]]]
    group_end = intervals[0].end
    for iv in intervals[1:]:
        if iv.start - group_end < CLOSE_AFTER:
            groups[-1].append(iv)
            group_end = max(group_end, iv.end)
        else:
            groups.append([iv])
            group_end = iv.end

    hotspot_by_slug = {v.slug: v.hotspot_ssid for v in vehicles}
    spans = []
    for g in groups:
        start = min(i.start for i in g)
        end = max(i.end for i in g)
        seconds: dict[str, int] = {}
        ssid_vehicles: dict[str, int] = {}
        for i in g:
            secs = int((i.end - i.start).total_seconds())
            seconds[i.kind] = seconds.get(i.kind, 0) + secs
            if i.kind == "hotspot_ssid" and i.vehicle_id:
                ssid_vehicles[i.vehicle_id] = ssid_vehicles.get(i.vehicle_id, 0) + secs
        if ssid_vehicles:
            vehicle, via = max(sorted(ssid_vehicles), key=lambda k: ssid_vehicles[k]), "hotspot_ssid"
        elif any(i.kind == "carplay" and i.vehicle_id for i in g):
            vehicle, via = phone.carplay_vehicle, "carplay_mapping"
        else:
            vehicle, via = None, None

        # Per-vehicle driver score over this span's own signals, using the
        # ported score_person on the span-sliced history.
        sliced = _slice_history(phone, history, start, end, window_end)
        scores = {slug: score_person(phone, sliced, hotspot)[0] for slug, hotspot in hotspot_by_slug.items()}
        base_score, _ = score_person(phone, sliced, None)
        spans.append(
            DetectedSpan(
                person=phone.person,
                vehicle_id=vehicle,
                started_at=start,
                ended_at=None if window_end - end < CLOSE_AFTER else end,
                evidence={
                    "signals": sorted(seconds),
                    "seconds": seconds,
                    "ssid_vehicles": ssid_vehicles,
                    "vehicle_via": via,
                    "scores": scores,
                    "base_score": base_score,
                    "last_signal_at": end.isoformat(),
                },
            )
        )
    return spans


def _slice_history(
    phone: PhoneConfig, history: dict[str, list[dict[str, Any]]], start: datetime, end: datetime, window_end: datetime
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for eid in phone.signal_entities():
        out[eid] = [{"state": v} for s, e, v in state_intervals(history.get(eid, []), window_end) if s < end and e > start]
    return out


def span_score(evidence: dict[str, Any], vehicle_id: str | None) -> int:
    """Driver score of a stored span for a given vehicle (see detect_spans)."""
    scores = evidence.get("scores") or {}
    if vehicle_id is not None and vehicle_id in scores:
        return int(scores[vehicle_id])
    return int(evidence.get("base_score") or 0)


# -------------------------------------------------------------- positions


def breadcrumbs_from_tracker(person: str, entries: Iterable[dict[str, Any]]) -> list[Position]:
    """Ported from trip_enricher.breadcrumbs_from_tracker, with one fix:
    the point time prefers last_updated. A device_tracker's last_changed
    only moves when its zone state changes, so while driving (state stays
    not_home) every GPS update shares one last_changed."""
    points: dict[datetime, Position] = {}
    for e in entries:
        attrs = e.get("attributes") or {}
        lat = parse_number(attrs.get("latitude"))
        lon = parse_number(attrs.get("longitude"))
        if lat is None or lon is None:
            continue
        ts = parse_ts(e.get("last_updated") or e.get("last_changed"))
        if ts is None:
            continue
        points[ts] = Position(person, ts, lat, lon, parse_number(attrs.get("speed")))
    return [points[t] for t in sorted(points)]
