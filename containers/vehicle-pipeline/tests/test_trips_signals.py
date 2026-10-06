from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from trips_helpers import ALICE, VEHICLES, state, t

from vehicle_pipeline.trips.detector import windows
from vehicle_pipeline.trips.signals import (
    breadcrumbs_from_tracker,
    detect_spans,
    extract_readings,
    score_person,
)
from vehicle_pipeline.trips.util import parse_ts

ODO = "sensor.car_a_mileage"


# ------------------------------------------------------------ readings


def test_reading_dedupe_on_capture_time_when_feed_re_reports() -> None:
    captured = t(7, 5).isoformat()
    rows = [
        state(ODO, "24042", t(9, 4), {"data_captured_at": captured}),
        state(ODO, "24042", t(9, 4), {"data_captured_at": captured, "age_minutes": 15}, updated=t(9, 19)),
        state(ODO, "24042", t(9, 4), {"data_captured_at": captured, "age_minutes": 30}, updated=t(9, 34)),
        state(ODO, "24057", t(12), {"data_captured_at": t(10, 2).isoformat()}),
    ]
    readings = extract_readings("id4", rows)
    assert [(r.km, r.data_captured_at) for r in readings] == [(Decimal("24042"), t(7, 5)), (Decimal("24057"), t(10, 2))]
    assert readings[0].ha_last_changed == t(9, 4)  # delivery time kept separately
    assert {r.source for r in readings} == {"ha_data_captured_at"}


def test_reading_without_capture_attribute_falls_back_to_last_changed() -> None:
    readings = extract_readings("id4", [state(ODO, "100.5", t(8)), state(ODO, "unavailable", t(9))])
    assert len(readings) == 1
    assert readings[0].data_captured_at == t(8)
    assert readings[0].source == "ha_last_changed"


def test_parse_ts_naive_is_utc() -> None:
    assert parse_ts("2026-09-01T08:00:00") == t(8)
    assert parse_ts("2026-09-01T10:00:00+02:00") == t(8)


# --------------------------------------------------------------- spans


def hist(**entities: list[tuple]) -> dict:
    """hist(ssid=[(value, at), ...], ...) for ALICE's entities."""
    ids = {"ssid": ALICE.ssid, "audio": ALICE.audio, "activity": ALICE.activity, "tracker": ALICE.tracker}
    return {ids[k]: [state(ids[k], v, at) for v, at in rows] for k, rows in entities.items()}


def test_hotspot_span_opens_and_closes() -> None:
    h = hist(ssid=[("home-wifi", t(7)), ("hotspot-a", t(8)), ("home-wifi", t(8, 40))])
    spans = detect_spans(ALICE, h, VEHICLES, window_end=t(10))
    assert len(spans) == 1
    s = spans[0]
    assert (s.started_at, s.ended_at, s.vehicle_id) == (t(8), t(8, 40), "id4")
    assert s.evidence["vehicle_via"] == "hotspot_ssid"
    assert s.evidence["scores"]["id4"] == 10 and s.evidence["scores"]["multivan"] == 0


def test_span_still_open_while_signal_continues_or_recently_ended() -> None:
    h = hist(audio=[("Speaker", t(7)), ("CarPlay", t(8))])
    assert detect_spans(ALICE, h, VEHICLES, window_end=t(9))[0].ended_at is None
    h = hist(audio=[("CarPlay", t(8)), ("Speaker", t(8, 57))])
    assert detect_spans(ALICE, h, VEHICLES, window_end=t(9))[0].ended_at is None  # 3 min ago
    h = hist(audio=[("CarPlay", t(8)), ("Speaker", t(8, 50))])
    assert detect_spans(ALICE, h, VEHICLES, window_end=t(9))[0].ended_at == t(8, 50)


def test_gaps_under_five_minutes_merge_longer_gaps_split() -> None:
    h = hist(audio=[("CarPlay", t(8)), ("Speaker", t(8, 10)), ("CarPlay", t(8, 13)), ("Speaker", t(8, 30))])
    spans = detect_spans(ALICE, h, VEHICLES, window_end=t(10))
    assert [(s.started_at, s.ended_at) for s in spans] == [(t(8), t(8, 30))]

    h = hist(audio=[("CarPlay", t(8)), ("Speaker", t(8, 10)), ("CarPlay", t(8, 17)), ("Speaker", t(8, 30))])
    spans = detect_spans(ALICE, h, VEHICLES, window_end=t(10))
    assert [(s.started_at, s.ended_at) for s in spans] == [(t(8), t(8, 10)), (t(8, 17), t(8, 30))]


def test_signals_of_different_kinds_chain_into_one_span() -> None:
    h = hist(
        activity=[("Automotive", t(7, 55)), ("Stationary", t(8, 35))],
        audio=[("CarPlay", t(8)), ("Speaker", t(8, 30))],
    )
    spans = detect_spans(ALICE, h, VEHICLES, window_end=t(10))
    assert len(spans) == 1
    assert (spans[0].started_at, spans[0].ended_at) == (t(7, 55), t(8, 35))
    assert spans[0].evidence["signals"] == ["automotive", "carplay"]


def test_ssid_beats_carplay_mapping_for_vehicle() -> None:
    phone = replace(ALICE, carplay_vehicle="id4")
    h = hist(ssid=[("hotspot-b", t(8)), ("x", t(8, 30))], audio=[("CarPlay", t(8)), ("Speaker", t(8, 30))])
    s = detect_spans(phone, h, VEHICLES, window_end=t(10))[0]
    assert (s.vehicle_id, s.evidence["vehicle_via"]) == ("multivan", "hotspot_ssid")
    assert s.evidence["scores"] == {"id4": 5, "multivan": 15}


def test_carplay_uses_mapping_or_stays_unresolved() -> None:
    h = hist(audio=[("CarPlay", t(8)), ("Speaker", t(8, 30))])
    mapped = detect_spans(replace(ALICE, carplay_vehicle="multivan"), h, VEHICLES, window_end=t(10))[0]
    assert (mapped.vehicle_id, mapped.evidence["vehicle_via"]) == ("multivan", "carplay_mapping")
    unmapped = detect_spans(ALICE, h, VEHICLES, window_end=t(10))[0]
    assert unmapped.vehicle_id is None


def test_automotive_alone_opens_candidate_without_vehicle() -> None:
    h = hist(activity=[("Walking", t(7)), ("Automotive", t(8)), ("Stationary", t(8, 30))])
    s = detect_spans(ALICE, h, VEHICLES, window_end=t(10))[0]
    assert s.vehicle_id is None
    assert s.evidence["base_score"] == 3


def test_unrelated_ssid_and_unknown_states_do_nothing() -> None:
    h = hist(ssid=[("someone-elses-wifi", t(8))], audio=[("unavailable", t(8))], activity=[("unknown", t(8))])
    assert detect_spans(ALICE, h, VEHICLES, window_end=t(10)) == []


@pytest.mark.parametrize(
    ("ssid", "audio", "activity", "expected"),
    [
        ("hotspot-a", "CarPlay", "Automotive", (18, ["hotspot_ssid", "carplay", "automotive"])),
        ("other", "CarPlay", "Stationary", (5, ["carplay"])),
        ("other", "Speaker", "Automotive", (3, ["automotive"])),
        ("other", "Speaker", "Stationary", (0, [])),
    ],
)
def test_score_person_weights(ssid: str, audio: str, activity: str, expected: tuple) -> None:
    h = hist(ssid=[(ssid, t(8))], audio=[(audio, t(8))], activity=[(activity, t(8))])
    assert score_person(ALICE, h, "hotspot-a") == expected


# ----------------------------------------------------------- positions


def test_breadcrumbs_use_last_updated_and_skip_points_without_coordinates() -> None:
    tracker = ALICE.tracker
    rows = [
        state(tracker, "not_home", t(8), {"latitude": 50.0, "longitude": 8.0, "speed": 30}, updated=t(8)),
        state(tracker, "not_home", t(8), {"latitude": 50.01, "longitude": 8.0}, updated=t(8, 1)),
        state(tracker, "not_home", t(8), {"gps_accuracy": 5}, updated=t(8, 2)),
    ]
    pts = breadcrumbs_from_tracker("alice", rows)
    assert [p.ts for p in pts] == [t(8), t(8, 1)]
    assert pts[0].speed_kmh == 30


# -------------------------------------------------------------- windows


def test_windows_backfill_in_day_chunks_with_overlap() -> None:
    w = list(windows(None, t(12, day=3), backfill_days=3))
    assert w[0][0] == t(12)
    assert w[-1][1] == t(12, day=3)
    assert len(w) == 4
    assert all(b[0] < a[1] for a, b in zip(w, w[1:], strict=False))  # chunks overlap


def test_windows_from_cursor_single_chunk() -> None:
    assert list(windows(t(8), t(8, 1), backfill_days=30)) == [(t(7, 50), t(8, 1))]
