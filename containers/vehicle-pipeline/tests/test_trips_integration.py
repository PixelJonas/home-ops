"""Trip detector against a real Postgres. Runs only with
INTEGRATION_DATABASE_URL set; every test works in a throwaway database it
creates (CREATE DATABASE) and drops, so the ``trips`` schema of the target
server is never touched. Skips when the role may not create databases.

Local run:
    podman run -d --rm --name vp-it -e POSTGRES_PASSWORD=pw -e PGDATA=/tmp/pgdata -p 127.0.0.1::5432 postgres:16
    INTEGRATION_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:<port>/postgres pytest tests/test_trips_integration.py
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool
from trips_helpers import ALICE, BOB, VEHICLES, state, t

from vehicle_pipeline.db import init_schema
from vehicle_pipeline.trips.detector import run_cycle
from vehicle_pipeline.trips.models import ReadingIn
from vehicle_pipeline.trips.schema import init_trips_schema
from vehicle_pipeline.trips.settings import TripSettings
from vehicle_pipeline.trips.store import TripStore

DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")


@pytest.fixture
def fresh_db() -> Iterator[str]:
    assert DATABASE_URL is not None
    name = f"trips_it_{uuid.uuid4().hex[:10]}"
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("role cannot CREATE DATABASE")
    params = conninfo_to_dict(DATABASE_URL)
    params["dbname"] = name
    try:
        yield make_conninfo(**params)
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def pool(fresh_db: str) -> Iterator[ConnectionPool]:
    p = ConnectionPool(fresh_db, min_size=1, max_size=4, open=True)
    try:
        yield p
    finally:
        p.close()


def _count(pool: ConnectionPool, sql: str, *args: Any) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return int(cur.fetchone()[0])  # type: ignore[index]


def test_schema_init_is_idempotent(pool: ConnectionPool) -> None:
    init_trips_schema(pool)
    init_trips_schema(pool)
    init_schema(pool)
    assert (
        _count(
            pool,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'trips' AND table_type = 'BASE TABLE'",
        )
        == 9
    )
    assert _count(pool, "SELECT count(*) FROM information_schema.views WHERE table_schema = 'trips'") == 1
    assert _count(pool, "SELECT count(*) FROM information_schema.views WHERE table_schema = 'vehicle_pipeline'") == 2


def test_concurrent_schema_init_from_app_and_detectors(fresh_db: str) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(fn: Any) -> None:
        p = ConnectionPool(fresh_db, min_size=1, max_size=1, open=True)
        try:
            barrier.wait()
            fn(p)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            p.close()

    threads = [threading.Thread(target=worker, args=(fn,)) for fn in [init_trips_schema, init_schema] * 3]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []


def test_reading_insert_dedupes_re_reports(pool: ConnectionPool) -> None:
    init_trips_schema(pool)
    store = TripStore(pool)
    r = ReadingIn("id4", Decimal("100"), t(8), t(10), "ha_data_captured_at")
    assert store.insert_readings([r, r]) == 1
    assert store.insert_readings([ReadingIn("id4", Decimal("100"), t(8), t(10, 15), "ha_data_captured_at")]) == 0


class FakeHA:
    """Serves canned entity histories with HA's semantics: rows whose
    last_changed falls in [start, end], preceded by the state at start
    re-stamped to start."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.calls: list[tuple[tuple[str, ...], datetime, datetime]] = []

    def fetch_history(self, entity_ids: list[str], start: datetime, end: datetime, *, significant_changes_only: bool = True) -> dict:
        self.calls.append((tuple(entity_ids), start, end))
        out = {}
        for eid in entity_ids:
            rows = sorted(self.rows.get(eid, []), key=lambda r: r["last_updated"])
            before = [r for r in rows if datetime.fromisoformat(r["last_updated"]) < start]
            inside = [r for r in rows if start <= datetime.fromisoformat(r["last_updated"]) <= end]
            res = []
            if before:
                res.append({**before[-1], "last_changed": start.isoformat(), "last_updated": start.isoformat()})
            res.extend(inside)
            if res:
                out[eid] = res
        return out


def _settings() -> TripSettings:
    return TripSettings(
        database_url="unused",
        ha_url="http://ha.example.test",
        ha_token="unused",
        vehicles=VEHICLES,
        phones=(ALICE, BOB),
    )


def test_detector_cycle_end_to_end(pool: ConnectionPool) -> None:
    init_trips_schema(pool)
    store = TripStore(pool)
    day = 0
    odo_a = "sensor.car_a_mileage"

    def odo(km: str, captured: datetime, delivered: datetime) -> dict[str, Any]:
        return state(odo_a, km, delivered, {"data_captured_at": captured.isoformat()})

    rows: dict[str, list[dict[str, Any]]] = {
        # Car A: parked at 10000 (re-reported), drive 08:00-08:30 -> 10025
        # delivered 3 h late; then 12:00-12:20 with no park snapshot yet.
        odo_a: [
            odo("10000", t(6, day=day), t(6, 30, day=day)),
            {**odo("10000", t(6, day=day), t(6, 30, day=day)), "last_updated": t(6, 45, day=day).isoformat()},
            odo("10025", t(8, 31, day=day), t(11, 30, day=day)),
        ],
        ALICE.ssid: [state(ALICE.ssid, "home", t(5)), state(ALICE.ssid, "hotspot-a", t(8)), state(ALICE.ssid, "home", t(8, 30))],
        ALICE.audio: [state(ALICE.audio, "Speaker", t(5)), state(ALICE.audio, "CarPlay", t(8, 1)), state(ALICE.audio, "Speaker", t(8, 29))],
        # Bob rides along (Automotive only), then drives car A alone at noon.
        BOB.activity: [
            state(BOB.activity, "Stationary", t(5)),
            state(BOB.activity, "Automotive", t(8, 2)),
            state(BOB.activity, "Stationary", t(8, 28)),
        ],
        BOB.ssid: [state(BOB.ssid, "home", t(5)), state(BOB.ssid, "hotspot-a", t(12)), state(BOB.ssid, "home", t(12, 20))],
        ALICE.tracker: [
            state(ALICE.tracker, "not_home", t(8), {"latitude": 50.0 + i * 0.01, "longitude": 8.0}, updated=t(8, i))
            for i in range(0, 30, 3)
        ],
    }
    ha = FakeHA(rows)
    settings = _settings()

    stats1 = run_cycle(settings, ha, store, t(13))
    assert stats1["readings_new"] == 2  # re-report collapsed
    assert _count(pool, "SELECT count(*) FROM trips.odometer_readings") == 2
    assert _count(pool, "SELECT count(*) FROM trips.drive_spans") == 3
    assert _count(pool, "SELECT count(*) FROM trips.trip_positions WHERE person = 'alice'") == 10

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT trip_key, vehicle_id, driver, km, status FROM trips.trips ORDER BY started_at")
        trips = cur.fetchall()
    assert len(trips) == 2
    assert trips[0][1:] == ("id4", "alice", Decimal("25.0"), "ok")
    assert trips[1][1:] == ("id4", "bob", None, "odometer_pending")
    log_after_first = _count(pool, "SELECT count(*) FROM trips.trip_projection_log")
    assert log_after_first == 2

    # Second cycle, nothing new: incremental windows, no projection churn.
    ha.calls.clear()
    run_cycle(settings, ha, store, t(13, 1))
    assert all(end - start <= timedelta(minutes=12) for _, start, end in ha.calls)
    assert _count(pool, "SELECT count(*) FROM trips.trip_projection_log") == log_after_first
    assert _count(pool, "SELECT count(*) FROM trips.drive_spans") == 3

    # Bob's park snapshot arrives late: the pending trip fills, logged.
    rows[odo_a].append(odo("10040", t(12, 21), t(14, 30)))
    run_cycle(settings, ha, store, t(14, 31))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT km, status FROM trips.trips WHERE driver = 'bob'")
        assert cur.fetchone() == (Decimal("15.0"), "ok")
        cur.execute("SELECT reason FROM trips.trip_projection_log ORDER BY id DESC LIMIT 1")
        assert cur.fetchone() == ("odometer_filled",)
