from __future__ import annotations

from decimal import Decimal

from trips_helpers import VEHICLES, reading, span, t, track

from vehicle_pipeline.trips.models import ProjectionResult, Trip
from vehicle_pipeline.trips.projection import project, split_tenths

SLUGS = [v.slug for v in VEHICLES]
WINDOW = t(0, day=-45)
NOW = t(23)


def run(spans=(), readings=(), positions=(), existing=None, now=NOW) -> ProjectionResult:
    return project(
        vehicles=SLUGS,
        spans=list(spans),
        readings=list(readings),
        positions=list(positions),
        existing=existing or {},
        now=now,
        window_start=WINDOW,
    )


def as_existing(result: ProjectionResult) -> dict[str, Trip]:
    return {tr.trip_key: tr for tr in result.trips}


def phone_trips(result: ProjectionResult) -> list[Trip]:
    return [tr for tr in result.trips if tr.source == "ha_phone"]


def test_single_trip_uses_bracketing_readings() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30))],
        readings=[reading(1, 10000, t(7, 55)), reading(2, 10025, t(8, 31))],
    )
    assert len(res.trips) == 1
    trip = res.trips[0]
    assert trip.trip_key == "span:1"
    assert (trip.odo_start, trip.odo_end, trip.km) == (Decimal("10000"), Decimal("10025"), Decimal("25.0"))
    assert trip.status == "ok"
    assert trip.driver == "alice"
    assert trip.vehicle_id == "id4"
    assert [entry.reason for entry in res.log] == ["created"]


def test_three_trips_in_one_bracket_split_by_duration_sum_exactly() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 20)), span(2, t(9), t(9, 30)), span(3, t(10), t(10, 10))],
        readings=[reading(1, 10000, t(7)), reading(2, 10050, t(10, 15))],
    )
    trips = phone_trips(res)
    assert [tr.status for tr in trips] == ["odometer_split"] * 3
    assert [tr.km for tr in trips] == [Decimal("16.7"), Decimal("25.0"), Decimal("8.3")]
    assert sum(tr.km for tr in trips) == Decimal("50")
    # odometer chain is contiguous from lo to hi
    assert trips[0].odo_start == Decimal("10000")
    assert trips[0].odo_end == trips[1].odo_start
    assert trips[1].odo_end == trips[2].odo_start
    assert trips[2].odo_end == Decimal("10050")
    assert all(tr.evidence["split_basis"] == "duration" for tr in trips)


def test_split_by_gps_when_every_trip_has_breadcrumbs() -> None:
    positions = (
        track("alice", t(8), t(8, 20), 10.0)
        + track("alice", t(9), t(9, 30), 30.0)
        + track("alice", t(10), t(10, 10), 10.0)
    )
    res = run(
        spans=[span(1, t(8), t(8, 20)), span(2, t(9), t(9, 30)), span(3, t(10), t(10, 10))],
        readings=[reading(1, 10000, t(7)), reading(2, 10049, t(10, 15))],
        positions=positions,
    )
    trips = phone_trips(res)
    assert [tr.km for tr in trips] == [Decimal("9.8"), Decimal("29.4"), Decimal("9.8")]
    assert sum(tr.km for tr in trips) == Decimal("49")
    assert trips[1].gps_km == Decimal("30.0")
    assert all(tr.evidence["split_basis"] == "gps" for tr in trips)


def test_split_tenths_largest_remainder() -> None:
    assert split_tenths(10, [1, 1, 1]) == [4, 3, 3]
    assert split_tenths(0, [5, 1]) == [0, 0]
    assert split_tenths(7, [0, 0]) == [4, 3]
    assert sum(split_tenths(1234, [3.3, 1.1, 7.7, 0.1])) == 1234


def test_missing_end_reading_is_pending_then_filled_with_log_row() -> None:
    s = [span(1, t(8), t(8, 30))]
    first = run(spans=s, readings=[reading(1, 10000, t(7))])
    trip = first.trips[0]
    assert trip.status == "odometer_pending"
    assert (trip.odo_start, trip.odo_end, trip.km) == (Decimal("10000"), None, None)

    second = run(
        spans=s,
        readings=[reading(1, 10000, t(7)), reading(2, 10012, t(11))],
        existing=as_existing(first),
    )
    assert second.trips[0].status == "ok"
    assert second.trips[0].km == Decimal("12.0")
    assert len(second.log) == 1
    entry = second.log[0]
    assert entry.reason == "odometer_filled"
    assert entry.old is not None and entry.old["status"] == "odometer_pending"
    assert entry.new is not None and entry.new["odo_end"] == "10012"


def test_open_span_is_pending() -> None:
    res = run(spans=[span(1, t(22), None)], readings=[reading(1, 10000, t(7))])
    assert res.trips[0].status == "odometer_pending"
    assert res.trips[0].ended_at is None


def test_mid_drive_reading_is_internal_not_parking() -> None:
    res = run(
        spans=[span(1, t(8), t(9))],
        readings=[reading(1, 10000, t(7)), reading(2, 10010, t(8, 30)), reading(3, 10040, t(9, 2))],
    )
    assert len(res.trips) == 1  # no odometer_gap trip for the internal pairs
    assert res.trips[0].km == Decimal("40.0")
    assert res.trips[0].status == "ok"


def test_park_snapshot_just_before_span_end_closes_the_trip() -> None:
    # The car snapshots at ignition-off; the phone signal lingers 2 minutes.
    res = run(
        spans=[span(1, t(8), t(8, 30))],
        readings=[reading(1, 10000, t(7)), reading(2, 10020, t(8, 28))],
    )
    assert res.trips[0].status == "ok"
    assert res.trips[0].km == Decimal("20.0")


def test_same_km_with_gps_movement_is_stale() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30))],
        readings=[reading(1, 10000, t(7)), reading(2, 10000, t(9))],
        positions=track("alice", t(8), t(8, 30), 5.0),
    )
    assert res.trips[0].status == "odometer_stale"
    assert res.trips[0].km == Decimal("0.0")


def test_same_km_without_gps_movement_is_ok() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30))],
        readings=[reading(1, 10000, t(7)), reading(2, 10000, t(9))],
        positions=track("alice", t(8), t(8, 30), 0.3),
    )
    assert res.trips[0].status == "ok"


def test_late_reading_between_trips_changes_split_with_log_rows() -> None:
    spans = [span(1, t(8), t(8, 30)), span(2, t(10), t(10, 30))]
    first = run(spans=spans, readings=[reading(1, 10000, t(7)), reading(3, 10040, t(11))])
    assert [tr.km for tr in first.trips] == [Decimal("20.0"), Decimal("20.0")]

    # A delayed park snapshot from 09:00 arrives: trip 1 was actually 30 km.
    second = run(
        spans=spans,
        readings=[reading(1, 10000, t(7)), reading(2, 10030, t(9)), reading(3, 10040, t(11))],
        existing=as_existing(first),
    )
    assert [(tr.km, tr.status) for tr in second.trips] == [(Decimal("30.0"), "ok"), (Decimal("10.0"), "ok")]
    assert len(second.log) == 2
    assert all(entry.reason.startswith("changed:") for entry in second.log)
    assert "km" in second.log[0].reason and "status" in second.log[0].reason


def test_recompute_is_idempotent() -> None:
    kwargs = {
        "spans": [span(1, t(8), t(8, 20)), span(2, t(9), t(9, 30))],
        "readings": [reading(1, 10000, t(7)), reading(2, 10050, t(10)), reading(3, 10090, t(12))],
    }
    first = run(**kwargs)
    second = run(**kwargs, existing=as_existing(first))
    assert second.upserts == [] and second.log == [] and second.removals == []
    assert second.trips == first.trips


def test_odometer_gap_without_span_becomes_synthetic_trip() -> None:
    res = run(readings=[reading(7, 10000, t(7)), reading(8, 10030, t(9)), reading(9, 10030, t(12))])
    assert len(res.trips) == 1
    gap = res.trips[0]
    assert gap.trip_key == "gap:id4:7"
    assert gap.source == "odometer_gap"
    assert gap.driver is None
    assert gap.km == Decimal("30.0")
    assert (gap.started_at, gap.ended_at) == (t(7), t(9))


def test_gap_after_covered_trip_is_still_counted() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30))],
        readings=[reading(1, 10000, t(7)), reading(2, 10020, t(8, 31)), reading(3, 10035, t(15))],
    )
    assert [(tr.trip_key, tr.km) for tr in res.trips] == [("span:1", Decimal("20.0")), ("gap:id4:2", Decimal("15.0"))]


def test_automotive_only_candidate_resolved_by_the_one_moving_odometer() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30), vehicle=None, automotive=True)],
        readings=[
            reading(1, 10000, t(7)),
            reading(2, 10015, t(9)),
            reading(3, 5000, t(7), vehicle="multivan"),
            reading(4, 5000, t(9), vehicle="multivan"),
        ],
    )
    assert len(res.trips) == 1
    trip = res.trips[0]
    assert trip.vehicle_id == "id4"
    assert trip.km == Decimal("15.0")
    assert trip.evidence["resolution"] == ["odometer"]
    assert res.stats["candidates_resolved_odometer"] == 1


def test_automotive_only_candidate_ambiguous_stays_unresolved_and_km_are_kept() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30), vehicle=None, automotive=True)],
        readings=[
            reading(1, 10000, t(7)),
            reading(2, 10015, t(9)),
            reading(3, 5000, t(7), vehicle="multivan"),
            reading(4, 5020, t(9), vehicle="multivan"),
        ],
    )
    assert phone_trips(res) == []
    assert sorted(tr.trip_key for tr in res.trips) == ["gap:id4:1", "gap:multivan:3"]
    assert res.stats["candidates_unresolved"] == 1


def test_short_automotive_flicker_is_ignored() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 1), vehicle=None, automotive=True)],
        readings=[reading(1, 10000, t(7)), reading(2, 10000, t(9))],
    )
    assert res.trips == []


def test_passenger_overlap_merges_into_one_trip_with_highest_scoring_driver() -> None:
    res = run(
        spans=[
            span(1, t(8, 1), t(8, 29), vehicle=None, person="alice", automotive=True),
            span(2, t(8), t(8, 30), vehicle="id4", person="bob", hotspot=True, carplay=True),
        ],
        readings=[reading(1, 10000, t(7)), reading(2, 10010, t(8, 31))],
    )
    assert len(res.trips) == 1
    trip = res.trips[0]
    assert trip.driver == "bob"
    assert trip.trip_key == "span:1"  # anchored on the lowest member span id
    assert trip.evidence["scores"] == {"alice": 3, "bob": 15}


def test_overlapping_spans_of_one_person_are_merged() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 20)), span(2, t(8, 10), t(8, 40))],
        readings=[reading(1, 10000, t(7)), reading(2, 10030, t(9))],
    )
    assert len(res.trips) == 1
    assert res.trips[0].ended_at == t(8, 40)
    assert res.trips[0].evidence["span_ids"] == [1, 2]


def test_trip_no_longer_produced_is_removed_with_log_row() -> None:
    first = run(spans=[span(1, t(8), t(8, 30))], readings=[reading(1, 10000, t(7)), reading(2, 10010, t(9))])
    second = run(readings=[reading(1, 10000, t(7)), reading(2, 10010, t(9))], existing=as_existing(first))
    assert second.removals == ["span:1"]
    assert [entry.reason for entry in second.log] == ["created", "removed"]  # gap trip appears instead


def test_trips_before_window_are_not_emitted() -> None:
    res = run(
        spans=[span(1, t(8, day=-50), t(9, day=-50))],
        readings=[reading(1, 10000, t(7, day=-50)), reading(2, 10010, t(10, day=-50))],
    )
    assert res.trips == []


def test_glitched_lower_reading_is_ignored() -> None:
    res = run(
        spans=[span(1, t(8), t(8, 30))],
        readings=[reading(1, 10000, t(7)), reading(2, 9000, t(7, 30)), reading(3, 10020, t(9))],
    )
    assert res.trips[0].km == Decimal("20.0")
