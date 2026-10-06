"""Import projection (trips.imports) and its integration into
trips.projection.project -- pure, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trips_helpers import VEHICLES, reading, span
from vw_helpers import EXPORT_A, EXPORT_B, T1, T2, T4, export_zip, inputs_from, r, scenario_zip

from vehicle_pipeline.trips.imports import (
    AnchorCheck,
    ImportInputs,
    project_imports,
    refuel_crosscheck,
)
from vehicle_pipeline.trips.models import ProjectionResult, Trip
from vehicle_pipeline.trips.projection import project

D = Decimal
SLUGS = [v.slug for v in VEHICLES]
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
WINDOW = NOW - timedelta(days=45)  # 2026-08-12: T1 (01.08.) is older


def z(dt: str) -> datetime:
    return datetime.fromisoformat(dt + "+00:00")


def odo(trips: list[Trip]) -> list[tuple[str, Decimal | None, Decimal | None, Decimal | None, str]]:
    return [(tr.ended_at.isoformat()[:16], tr.odo_start, tr.odo_end, tr.km, tr.status) for tr in trips]  # type: ignore[union-attr]


def run(imports: ImportInputs, spans=(), readings=(), existing=None) -> ProjectionResult:  # type: ignore[no-untyped-def]
    return project(
        vehicles=SLUGS,
        spans=list(spans),
        readings=list(readings),
        positions=[],
        existing=existing or {},
        now=NOW,
        window_start=WINDOW,
        imports=imports,
    )


# ----------------------------------------------------------------- chaining


def test_single_export_chains_backwards_from_the_anchor() -> None:
    res = project_imports(inputs_from(scenario_zip(EXPORT_A)))
    assert odo(res.trips) == [
        ("2026-08-01T10:00", D("10000"), D("10100"), D("100.0"), "ok"),
        ("2026-09-10T07:00", D("10100"), D("10150"), D("50.0"), "ok"),
        ("2026-09-15T16:00", D("10150"), D("10300"), D("150.0"), "ok"),
    ]
    first, last = res.trips[0], res.trips[-1]
    assert sum(tr.km for tr in res.trips) == last.odo_end - first.odo_start  # type: ignore[operator]
    assert first.trip_key == "vw:multivan:2026-08-01T10:00:00+00:00"
    assert first.started_at == z("2026-08-01T08:30")  # end - 01:30 driving time
    assert (first.source, first.driver, first.gps_km) == ("vw_export", None, None)
    ev = last.evidence
    assert ev["start_estimated"] and ev["odometer_derived"] and "drift" in ev["odometer_rounding"]
    assert ev["anchor"]["check"] == "no_reading" and ev["anchor"]["odometer_km"] == "10300"
    assert res.coverage == {"multivan": [(z("2026-08-01T08:30"), z("2026-09-15T16:00"))]}


def test_newer_overlapping_export_owns_the_shared_rows() -> None:
    res = project_imports(inputs_from(scenario_zip(EXPORT_A), scenario_zip(EXPORT_B)))
    assert odo(res.trips) == [
        ("2026-08-01T10:00", D("10000"), D("10100"), D("100.0"), "ok"),  # only in A
        ("2026-09-10T07:00", D("10100"), D("10150"), D("50.0"), "ok"),  # B
        ("2026-09-15T16:00", D("10150"), D("10300"), D("150.0"), "ok"),  # B
        ("2026-09-22T06:00", D("10300"), D("10420"), D("120.0"), "ok"),  # B
    ]
    assert [tr.evidence["exports"] for tr in res.trips] == [1, 2, 2, 1]
    assert [tr.evidence["anchor"]["export_id"] for tr in res.trips] == [1, 2, 2, 2]


def test_reading_cross_check() -> None:
    a, b = scenario_zip(EXPORT_A), scenario_zip(EXPORT_B)
    park = reading(9, 10420, z("2026-09-22T06:02"), vehicle="multivan")
    ok = project_imports(inputs_from(a, b, checks={2: AnchorCheck(park)}))
    assert {tr.evidence["anchor"]["check"] for tr in ok.trips[1:]} == {"verified"}
    assert all(tr.status == "ok" for tr in ok.trips)

    off = reading(9, 10431, z("2026-09-22T06:02"), vehicle="multivan")
    bad = project_imports(inputs_from(a, b, checks={2: AnchorCheck(off)}))
    assert [tr.status for tr in bad.trips] == ["ok", "odometer_pending", "odometer_pending", "odometer_pending"]
    t4 = bad.trips[-1]
    assert (t4.odo_start, t4.odo_end, t4.km) == (None, None, D("120.0"))
    assert t4.evidence["anchor"]["check"] == "mismatch" and t4.evidence["derived_odo_end"] == "10420"

    # A span started in between: the reading cannot verify the anchor.
    between = project_imports(inputs_from(a, b, checks={2: AnchorCheck(off, span_between=True)}))
    assert all(tr.status == "ok" for tr in between.trips)
    assert between.trips[-1].evidence["anchor"]["check"] == "unverifiable_trip_between"


def test_merged_trip_in_newer_export_replaces_the_older_row() -> None:
    # A was exported right after T3; VW then merged T3 with the next leg
    # (stop < 2 h): the newer export has one 190 km trip ending 21:00.
    a = scenario_zip({**EXPORT_A, "created": "15/09/2026, 18:30"})
    t3m = r("15/09/2026, 21:00", "190", "02:40")
    b = export_zip([T2, t3m, T4], odometer="10,460 km", created="25/09/2026, 12:00")
    res = project_imports(inputs_from(a, b))
    assert odo(res.trips) == [
        ("2026-08-01T10:00", D("10000"), D("10100"), D("100.0"), "ok"),
        ("2026-09-10T07:00", D("10100"), D("10150"), D("50.0"), "ok"),
        ("2026-09-15T19:00", D("10150"), D("10340"), D("190.0"), "ok"),
        ("2026-09-22T06:00", D("10340"), D("10460"), D("120.0"), "ok"),
    ]
    assert res.stats["imported_rewritten"] == 1


def test_trip_still_open_at_export_time_invalidates_the_anchor() -> None:
    # A newer export shows a trip that ended before A was created but is not
    # in A (it was still being merged): A's odometer included it.
    late = r("20/09/2026, 21:30", "30", "00:30")
    a = scenario_zip(EXPORT_A)
    b = export_zip([T2, *EXPORT_A["short"][2:], late], odometer="10,330 km", created="25/09/2026, 12:00")
    res = project_imports(inputs_from(a, b))
    by_end = {tr.ended_at.isoformat()[:10]: tr for tr in res.trips}  # type: ignore[union-attr]
    assert by_end["2026-08-01"].status == "odometer_pending"  # A-only row
    assert by_end["2026-08-01"].evidence["anchor"]["check"] == "trip_after_last"
    assert by_end["2026-09-20"].odo_end == D("10330") and by_end["2026-09-20"].status == "ok"


# --------------------------------------------------------- supersede in project


def _mv_span(sid: int, start: datetime, end: datetime, vehicle: str | None = "multivan", person: str = "alice"):  # type: ignore[no-untyped-def]
    return span(sid, start, end, vehicle=vehicle, person=person)


def _imports() -> ImportInputs:
    return inputs_from(scenario_zip(EXPORT_A), scenario_zip(EXPORT_B))


def test_detector_trips_inside_coverage_are_superseded() -> None:
    spans = [
        _mv_span(1, z("2026-09-15T14:05"), z("2026-09-15T16:03")),  # = T3 (+3 min phone lag)
        _mv_span(2, z("2026-09-25T14:00"), z("2026-09-25T14:30")),  # after coverage
        _mv_span(3, z("2026-09-15T14:10"), z("2026-09-15T15:00"), vehicle="id4", person="bob"),  # other vehicle
    ]
    readings = [reading(1, 10420, z("2026-09-22T06:02"), vehicle="multivan")]
    res = run(_imports(), spans=spans, readings=readings)
    keys = {tr.trip_key: tr for tr in res.trips}
    assert "span:1" not in keys
    assert keys["span:2"].source == "ha_phone" and keys["span:3"].vehicle_id == "id4"
    t3 = keys["vw:multivan:2026-09-15T16:00:00+00:00"]
    assert t3.evidence["supersedes"] == ["span:1"]
    assert res.stats["superseded"] == 1
    # The imported trip older than the trailing window is projected too.
    assert "vw:multivan:2026-08-01T10:00:00+00:00" in keys
    # The odometer_gap trip 10420 -> nothing yet; no km counted twice:
    mv_km = sum(tr.km for tr in res.trips if tr.vehicle_id == "multivan" and tr.status == "ok")  # type: ignore[misc]
    assert mv_km == D("420.0")

    # Stored detector trip superseded later (import arrives after detection):
    existing = {"span:1": Trip("span:1", "multivan", "alice", z("2026-09-15T14:05"), z("2026-09-15T16:03"),
                               None, None, None, None, "odometer_pending", "ha_phone")}
    res2 = run(_imports(), spans=spans, readings=readings, existing=existing)
    assert res2.removals == ["span:1"]
    assert [e.reason for e in res2.log if e.trip_key == "span:1"] == ["superseded:vw:multivan:2026-09-15T16:00:00+00:00"]


def test_recompute_is_stable_and_supersedes_is_cumulative() -> None:
    spans = [_mv_span(1, z("2026-09-15T14:05"), z("2026-09-15T16:03"))]
    first = run(_imports(), spans=spans)
    existing = {tr.trip_key: tr for tr in first.trips}
    again = run(_imports(), spans=spans, existing=existing)
    assert (again.upserts, again.removals, again.log) == ([], [], [])
    # The span is gone (e.g. aged out); the mapping stays.
    later = run(_imports(), spans=[], existing=existing)
    assert (later.upserts, later.removals, later.log) == ([], [], [])


def test_old_stored_detector_trips_outside_the_window() -> None:
    old_inside = Trip("gap:multivan:1", "multivan", None, z("2026-08-01T08:40"), z("2026-08-01T09:50"),
                      D("9990"), D("10090"), D("100.0"), None, "ok", "odometer_gap")
    old_outside = Trip("span:7", "multivan", "bob", z("2026-07-01T08:00"), z("2026-07-01T09:00"),
                       None, None, None, None, "odometer_pending", "ha_phone")
    res = run(_imports(), existing={"gap:multivan:1": old_inside, "span:7": old_outside})
    assert res.removals == ["gap:multivan:1"]  # superseded; span:7 untouched (outside coverage and window)
    t1 = next(tr for tr in res.trips if tr.trip_key == "vw:multivan:2026-08-01T10:00:00+00:00")
    assert t1.evidence["supersedes"] == ["gap:multivan:1"]


def test_without_imports_projection_is_unchanged() -> None:
    spans = [_mv_span(1, z("2026-09-15T14:05"), z("2026-09-15T16:03"))]
    assert [tr.trip_key for tr in run(ImportInputs(), spans=spans).trips] == ["span:1"]


# ------------------------------------------------------------- refuel check


def test_refuel_crosscheck() -> None:
    # Refuel 1 during T3 (merged stop), refuel 2 between T3 and T4; the last
    # segment is the open one since refuel 2 and ends with T4.
    short = [T1, T2, r("15/09/2026, 18:00", "150", "02:00"), T4]
    # Chained back from the anchor 10,420: open 170 -> refuel 3 at 10250,
    # 40 -> refuel 2 at 10210, 110 -> refuel 1 at 10100.
    refuel = [
        r("01/08/2026, 12:00", "300", "05:00"),  # at T1's end (bracket 10000..10100)
        r("15/09/2026, 17:00", "110", "02:00"),  # during T3 (bracket 10150..10300)
        r("16/09/2026, 10:00", "40", "00:30"),  # between T3 and T4: T3 ends at 10300 -> -50 km
        r("22/09/2026, 08:00", "170", "01:10"),  # open segment since refuel 3
    ]
    zz = export_zip(short, refuel=refuel, odometer="10,420 km", created="25/09/2026, 12:00")
    inputs = inputs_from(zz)
    trips = project_imports(inputs).trips
    checks = refuel_crosscheck(inputs.exports, inputs.rows, trips)
    assert [(c.position, c.odometer_from_segments, c.diff_km) for c in checks] == [
        ("inside", D("10100"), D("0")),
        ("inside", D("10210"), D("0")),
        ("between", D("10250"), D("-50")),
    ]
    assert checks[0].refuel_at == z("2026-08-01T10:00") and checks[1].trip_key.endswith("2026-09-15T16:00:00+00:00")  # type: ignore[union-attr]
