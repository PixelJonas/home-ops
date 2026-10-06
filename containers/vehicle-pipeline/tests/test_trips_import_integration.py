"""VW export import against a real Postgres (throwaway database per test,
see test_trips_integration for the local podman recipe): idempotent
import, VIN mapping, the detector cycle projecting/superseding, the
vollkosten views, annotation carry-over and notifications."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from test_trips_integration import DATABASE_URL, FakeHA, _settings, fresh_db, pool  # noqa: F401 - pytest fixtures
from trips_helpers import scores
from vw_helpers import EXPORT_A, EXPORT_B, T1, VIN_ID4, VIN_MV, VIN_UNKNOWN, export_zip, r, scenario_zip

from vehicle_pipeline.config import VehicleConfig
from vehicle_pipeline.db import init_schema, upsert_vehicles
from vehicle_pipeline.trips import import_vw
from vehicle_pipeline.trips.detector import run_cycle
from vehicle_pipeline.trips.models import ReadingIn
from vehicle_pipeline.trips.store import TripStore
from vehicle_pipeline.vollkosten import load_vollkosten

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")

D = Decimal
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
T3_KEY = "vw:multivan:2026-09-15T16:00:00+00:00"


def z(dt: str) -> datetime:
    return datetime.fromisoformat(dt + "+00:00")


@pytest.fixture
def store(pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch) -> TripStore:  # noqa: F811
    monkeypatch.setenv("TZ", "Europe/Berlin")
    init_schema(pool)
    upsert_vehicles(
        pool,
        [
            VehicleConfig(slug="id4", vin=VIN_ID4, label="Test ID.4"),
            VehicleConfig(slug="multivan", vin=VIN_MV, label="Test Multivan"),
        ],
    )
    return TripStore(pool)


def _import(url: str, data: bytes, dry_run: bool = False) -> dict[str, int]:
    return import_vw.run_import(io.BytesIO(data), database_url=url, dry_run=dry_run, tz="Europe/Berlin",
                                out=io.StringIO())


def _q(pool: ConnectionPool, sql: str, *args: Any) -> list[tuple]:  # noqa: F811
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _span(pool: ConnectionPool, person: str, vehicle: str, start: datetime, end: datetime) -> int:  # noqa: F811
    rows = _q(
        pool,
        """
        INSERT INTO trips.drive_spans (person, vehicle_id, started_at, ended_at, evidence, source, detector_version)
        VALUES (%s, %s, %s, %s, %s, 'ha_phone', 1) RETURNING id
        """,
        person, vehicle, start, end, Jsonb(scores(vehicle, hotspot=True, carplay=True)),
    )
    return int(rows[0][0])


def test_import_is_idempotent_across_overlapping_exports(pool: ConnectionPool, fresh_db: str, store: TripStore) -> None:  # noqa: F811
    a = scenario_zip(EXPORT_A, refuel=[r("10/09/2026, 09:00", "400")], long=[r("15/09/2026, 18:00", "300")])
    assert _import(fresh_db, a) == {"rows": 5, "inserted": 5, "already_known": 0, "export_new": 1}
    assert _import(fresh_db, a) == {"rows": 5, "inserted": 0, "already_known": 5, "export_new": 0}
    b = scenario_zip(EXPORT_B)
    assert _import(fresh_db, b, dry_run=True) == {"rows": 3, "would_insert": 1, "already_known": 2}
    assert _q(pool, "SELECT count(*) FROM trips.import_exports") == [(1,)]  # dry run wrote nothing
    assert _import(fresh_db, b) == {"rows": 3, "inserted": 1, "already_known": 2, "export_new": 1}
    assert _q(pool, "SELECT kind, count(*) FROM trips.imported_trips GROUP BY 1 ORDER BY 1") == [
        ("long_term", 1), ("refuel_segment", 1), ("short_term", 4)]
    assert _q(pool, "SELECT count(*) FROM trips.imported_trip_exports") == [(5 + 3,)]
    row = _q(pool, "SELECT vehicle_id, ended_at, km, driving_time, consumption_unit, export_odometer_km "
                   "FROM trips.imported_trips WHERE kind = 'short_term' ORDER BY ended_at LIMIT 1")[0]
    assert row == ("multivan", z("2026-08-01T10:00"), D("100"), timedelta(hours=1, minutes=30), "l/100 km", D("10300"))
    # The VIN is never stored.
    dump = _q(pool, "SELECT string_agg(t::text, ' ') FROM trips.imported_trips t")[0][0]
    dump += _q(pool, "SELECT string_agg(e::text, ' ') FROM trips.import_exports e")[0][0]
    assert VIN_MV not in dump


def test_unknown_vin_aborts_without_writing(pool: ConnectionPool, fresh_db: str, store: TripStore,  # noqa: F811
                                            monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    data = export_zip([T1], vin=VIN_UNKNOWN)
    with pytest.raises(import_vw.ImportFailed, match="not in vehicle_pipeline.vehicles"):
        _import(fresh_db, data)
    path = tmp_path / "x.zip"
    path.write_bytes(data)
    monkeypatch.setenv("DATABASE_URL", fresh_db)
    assert import_vw.main([str(path)]) == 2
    assert VIN_UNKNOWN not in capsys.readouterr().err  # masked
    assert _q(pool, "SELECT count(*) FROM trips.imported_trips") == [(0,)]


def _scenario(pool: ConnectionPool, url: str, store: TripStore) -> dict[str, int]:  # noqa: F811
    refuel = [r("15/09/2026, 17:00", "110", "02:00"), r("22/09/2026, 08:00", "170")]
    _import(url, scenario_zip(EXPORT_A))
    _import(url, scenario_zip(EXPORT_B, refuel=refuel))
    store.insert_readings([
        ReadingIn("multivan", D(km), z(at), None, "ha_data_captured_at")
        for km, at in (("10148", "2026-09-15T13:00"), ("10300", "2026-09-15T16:30"), ("10420", "2026-09-22T06:02"))
    ])
    return {
        "mv": _span(pool, "alice", "multivan", z("2026-09-15T14:05"), z("2026-09-15T16:03")),  # = T3
        "id4": _span(pool, "bob", "id4", z("2026-09-15T14:10"), z("2026-09-15T15:00")),
    }


def test_cycle_projects_imports_supersedes_and_is_stable(pool: ConnectionPool, fresh_db: str, store: TripStore) -> None:  # noqa: F811
    spans = _scenario(pool, fresh_db, store)
    stats = run_cycle(_settings(), FakeHA({}), store, NOW)
    assert stats["superseded"] == 2  # the T3 span trip and the 10300 -> 10420 odometer gap

    trips = _q(pool, "SELECT trip_key, odo_start, odo_end, km, status, source, evidence->'anchor'->>'check' "
                     "FROM trips.trips WHERE vehicle_id = 'multivan' ORDER BY started_at")
    assert [(k, s, e, km, st) for k, s, e, km, st, _src, _c in trips] == [
        ("vw:multivan:2026-08-01T10:00:00+00:00", D("10000"), D("10100"), D("100.0"), "ok"),  # older than 45 days
        ("vw:multivan:2026-09-10T07:00:00+00:00", D("10100"), D("10150"), D("50.0"), "ok"),
        (T3_KEY, D("10150"), D("10300"), D("150.0"), "ok"),
        ("vw:multivan:2026-09-22T06:00:00+00:00", D("10300"), D("10420"), D("120.0"), "ok"),
    ]
    assert {c for *_x, c in trips} == {"verified"}
    assert _q(pool, "SELECT trip_key FROM trips.trips WHERE vehicle_id = 'id4'") == [(f"span:{spans['id4']}",)]
    assert _q(pool, "SELECT evidence->'supersedes' FROM trips.trips WHERE trip_key = %s", T3_KEY)[0][0] == [
        f"span:{spans['mv']}"]

    # Stable: further cycles write no projection log rows.
    log = _q(pool, "SELECT count(*) FROM trips.trip_projection_log")[0][0]
    run_cycle(_settings(), FakeHA({}), store, NOW + timedelta(minutes=1))
    run_cycle(_settings(), FakeHA({}), store, NOW + timedelta(days=60))  # all of it outside the window now
    assert _q(pool, "SELECT count(*) FROM trips.trip_projection_log")[0][0] == log
    assert _q(pool, "SELECT count(*) FROM trips.trips WHERE source = 'vw_export'") == [(4,)]

    # Vollkosten counts the imported km once.
    monthly = {(row["vehicle_id"], str(row["period_start"])): row for row in load_vollkosten(pool, "monthly")}
    sep, aug = monthly[("multivan", "2026-09-01")], monthly[("multivan", "2026-08-01")]
    assert (sep["km"], sep["trips_total"], sep["trips_km_unknown"]) == (D("320.0"), 3, 0)
    assert aug["km"] == D("100.0")

    # Refuel odometer cross-check.
    (check,) = store.refuel_check("multivan")
    assert (check.position, check.odometer_from_segments, check.diff_km) == ("inside", D("10250"), D("0"))


def test_annotations_carry_over_and_imports_are_never_notified(pool: ConnectionPool, fresh_db: str,  # noqa: F811
                                                               store: TripStore) -> None:
    spans = _scenario(pool, fresh_db, store)
    store.annotate(f"span:{spans['mv']}", business=True, purpose="Kunde", via="notify", actor="ha")
    run_cycle(_settings(), FakeHA({}), store, NOW)
    t3 = store.get_effective(T3_KEY)
    assert t3 is not None and (t3.business, t3.business_via, t3.purpose) == (True, "notify", "Kunde")
    store.annotate(T3_KEY, business=False, via="ui")  # its own annotation wins
    t3 = store.get_effective(T3_KEY)
    assert t3 is not None and (t3.business, t3.purpose) == (False, "Kunde")

    cutoff = z("2026-07-01T00:00")  # before every imported trip
    candidates = store.notify_candidates(cutoff)
    assert [c.trip_key for c in candidates] == [f"span:{spans['id4']}"]
    assert _q(pool, "SELECT count(*) FROM trips.trips WHERE source = 'vw_export' AND ended_at > %s", cutoff) == [(4,)]


def test_imported_trips_end_before_the_live_notify_cutoff(store: TripStore, fresh_db: str) -> None:  # noqa: F811
    # The detector's persisted cutoff is its first notify-enabled start;
    # every trip of an export taken before then ends before it.
    cutoff = store.ensure_notify_cutoff(z("2026-09-26T00:00"))
    _import(fresh_db, scenario_zip(EXPORT_B))
    run_cycle(_settings(), FakeHA({}), store, NOW)
    assert all(c.source != "vw_export" for c in store.notify_candidates(cutoff))
