"""annotate() + trips.trips_effective, the notification store path and
the Vollkostenrechnung views against a real Postgres (throwaway database
per test, see test_trips_integration for the local podman recipe)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from psycopg_pool import ConnectionPool
from test_trips_integration import DATABASE_URL, fresh_db, pool  # noqa: F401 - pytest fixtures

from vehicle_pipeline.config import VehicleConfig
from vehicle_pipeline.db import init_schema, upsert_vehicles
from vehicle_pipeline.trips.notify import Notifier, notify_id
from vehicle_pipeline.trips.store import AnnotationError, TripStore
from vehicle_pipeline.vollkosten import load_vollkosten

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")

D = Decimal


def _insert_trip(
    db: ConnectionPool,
    key: str,
    started: datetime,
    *,
    vehicle: str = "id4",
    km: str | None = None,
    gps_km: str | None = None,
    status: str = "ok",
    ended: datetime | None | str = "auto",
    driver: str | None = "alice",
) -> None:
    end = started + timedelta(minutes=30) if ended == "auto" else ended
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trips.trips (trip_key, vehicle_id, driver, started_at, ended_at, km, gps_km, status, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ha_phone')
            """,
            (key, vehicle, driver, started, end, D(km) if km else None, D(gps_km) if gps_km else None, status),
        )


def _cost(db: ConnectionPool, ref: str, day: str, category: str, amount: str, vehicle: str = "id4") -> None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vehicle_pipeline.costs (vehicle_id, date, category, amount_gross, source, source_ref) "
            "VALUES (%s, %s, %s, %s, 'manual', %s)",
            (vehicle, day, category, D(amount), ref),
        )


@pytest.fixture
def schema(pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch) -> TripStore:  # noqa: F811
    monkeypatch.setenv("TZ", "Europe/Berlin")  # period bucketing of the vollkosten views
    init_schema(pool)
    upsert_vehicles(
        pool,
        [
            VehicleConfig(slug="id4", vin="VINFAKE00000000A1", label="Test ID.4"),
            VehicleConfig(slug="multivan", vin="VINFAKE00000000B2", label="Test Multivan"),
        ],
    )
    return TripStore(pool)


def at(day: str, hm: str = "08:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+00:00")


# ------------------------------------------------------- annotate / effective


def test_annotate_latest_wins_and_overrides_apply(pool: ConnectionPool, schema: TripStore) -> None:  # noqa: F811
    store = schema
    _insert_trip(pool, "span:1", at("2026-10-01"), vehicle="multivan", km="12.5")
    assert store.annotate("span:1", business=True, via="notify", actor="ha") == 1
    assert store.annotate(["span:1"], business=False, purpose="Einkauf", via="ui", actor="ui") == 2
    store.annotate("span:1", driver="bob", vehicle="id4", via="api")
    t = store.get_effective("span:1")
    assert t is not None
    assert (t.business, t.business_via, t.purpose) == (False, "ui", "Einkauf")
    assert (t.driver, t.vehicle_id, t.detected_driver, t.detected_vehicle_id) == ("bob", "id4", "alice", "multivan")

    # A null annotation clears the field; history stays append-only.
    store.annotate("span:1", business=None, driver=None, via="ui")
    t = store.get_effective("span:1")
    assert t is not None and t.business is None and t.driver == "alice"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trips.trip_annotations WHERE trip_key = 'span:1'")
        assert cur.fetchone()[0] == 7  # type: ignore[index]


def test_annotate_rejects_bad_input_atomically(pool: ConnectionPool, schema: TripStore) -> None:  # noqa: F811
    store = schema
    _insert_trip(pool, "span:1", at("2026-10-01"))
    for kwargs in ({"business": "yes"}, {"colour": "red"}, {}):
        with pytest.raises(AnnotationError):
            store.annotate("span:1", via="ui", **kwargs)
    with pytest.raises(AnnotationError):
        store.annotate("span:1", business=True, via="email")
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trips.trip_annotations")
        assert cur.fetchone()[0] == 0  # type: ignore[index]


def test_list_effective_filters(pool: ConnectionPool, schema: TripStore) -> None:  # noqa: F811
    store = schema
    _insert_trip(pool, "span:1", at("2026-09-30", "23:30"), km="5")  # 01.10. 01:30 Berlin
    _insert_trip(pool, "span:2", at("2026-10-02"), status="odometer_pending", gps_km="7")
    _insert_trip(pool, "span:3", at("2026-10-03"), vehicle="multivan", km="9")
    store.annotate("span:2", business=True, via="ui")
    from vehicle_pipeline.trips_ui import month_bounds

    start, end = month_bounds("2026-10")
    keys = lambda **kw: [t.trip_key for t in store.list_effective(**kw)]  # noqa: E731
    assert keys(month_start=start, month_end=end) == ["span:3", "span:2", "span:1"]
    assert keys(vehicle="id4", unflagged_only=True) == ["span:1"]
    assert keys(status="odometer_pending") == ["span:2"]
    assert store.vehicle_ids() == ["id4", "multivan"]


# ------------------------------------------------------------ notifications


class _HA:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.calls.append((domain, service, data))


def test_notify_end_to_end_with_persisted_cutoff(pool: ConnectionPool, schema: TripStore) -> None:  # noqa: F811
    from zoneinfo import ZoneInfo

    from vehicle_pipeline.trips.notify import handle_event

    store = schema
    first_start = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
    _insert_trip(pool, "span:backfilled", first_start - timedelta(hours=3))
    assert store.ensure_notify_cutoff(first_start) == first_start
    # A later start never moves the cutoff.
    assert store.ensure_notify_cutoff(first_start + timedelta(days=1)) == first_start

    now = first_start + timedelta(hours=2)
    _insert_trip(pool, "span:new", now - timedelta(minutes=50))  # ended 20 min ago
    _insert_trip(pool, "span:open", now - timedelta(minutes=20), ended=None)
    _insert_trip(pool, "span:flagged", now - timedelta(minutes=60))
    store.annotate("span:flagged", business=False, via="ui")

    ha = _HA()
    n = Notifier(store=store, ha=ha, targets=["mobile_app_test_phone"], vehicle_names={}, tz=ZoneInfo("UTC"))
    assert n.start(first_start + timedelta(hours=1)) == first_start
    assert n.run_once(now) == {"notify_sent": 1, "notify_failed": 0}
    assert [c[2]["data"]["tag"] for c in ha.calls] == [f"trip_{notify_id('span:new')}"]
    assert n.run_once(now)["notify_sent"] == 0  # already sent
    assert store.trip_key_for_notify_id(notify_id("span:new")) == "span:new"

    event = {
        "type": "event",
        "event": {"event_type": "mobile_app_notification_action", "data": {"action": f"TRIP_BIZ_{notify_id('span:new')}"}},
    }
    assert handle_event(event, store) == "span:new"
    t = store.get_effective("span:new")
    assert t is not None and t.business is True and t.business_via == "notify"


# --------------------------------------------------------------- vollkosten


def _by_period(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["vehicle_id"], r["period_start"].isoformat()): r for r in rows}


def test_vollkosten_views_math(pool: ConnectionPool, schema: TripStore) -> None:  # noqa: F811
    store = schema
    _cost(pool, "c1", "2026-01-05", "fuel", "100.00")
    _cost(pool, "c2", "2026-01-28", "financing", "300.00")
    _cost(pool, "c3", "2026-02-10", "insurance", "600.00")

    _insert_trip(pool, "A", at("2026-01-10"), km="100")
    _insert_trip(pool, "B", at("2026-01-12"), km="50", status="odometer_split")
    _insert_trip(pool, "C", at("2026-01-20"), gps_km="30", status="odometer_pending")
    _insert_trip(pool, "D", at("2026-01-31", "23:30"), km="200")  # 01.02. 00:30 Berlin -> February
    _insert_trip(pool, "E", at("2026-02-02"), km="100")
    _insert_trip(pool, "F", at("2026-02-03"), km="100", vehicle="multivan")  # re-assigned to id4 below
    for key in ("A", "C"):
        store.annotate(key, business=True, via="ui")
    store.annotate("D", business=False, via="ui")
    store.annotate("E", business=False, via="notify")
    store.annotate("E", business=True, via="ui")  # latest wins
    store.annotate("F", business=True, vehicle="id4", via="ui")

    monthly = _by_period(load_vollkosten(pool, "monthly"))
    assert set(monthly) == {("id4", "2026-01-01"), ("id4", "2026-02-01")}
    jan = monthly[("id4", "2026-01-01")]
    assert (jan["cost_total"], jan["cost_financing"], jan["cost_excl_financing"], jan["cost_fuel"]) == (
        D("400.00"),
        D("300.00"),
        D("100.00"),
        D("100.00"),
    )
    assert (jan["km"], jan["km_business"], jan["km_unflagged"]) == (D("150"), D("100"), D("50"))
    assert (jan["km_gps_unconfirmed"], jan["km_business_gps_unconfirmed"]) == (D("30"), D("30"))
    assert (jan["trips_total"], jan["trips_km_unknown"], jan["trips_unflagged"], jan["trips_business"]) == (3, 1, 1, 2)
    assert jan["cost_per_km"] == D("2.6667")
    assert jan["business_cost"] == D("266.67")
    assert jan["business_cost_excl_financing"] == D("66.67")
    assert jan["business_share"] == D("0.6667")
    assert jan["complete"] is False

    feb = monthly[("id4", "2026-02-01")]
    assert (feb["cost_total"], feb["cost_insurance"], feb["km"], feb["km_business"]) == (
        D("600.00"),
        D("600.00"),
        D("400"),
        D("200"),
    )
    assert (feb["cost_per_km"], feb["business_cost"], feb["complete"]) == (D("1.5000"), D("300.00"), True)

    yearly = _by_period(load_vollkosten(pool, "yearly"))
    y = yearly[("id4", "2026-01-01")]
    assert (y["cost_total"], y["cost_excl_financing"], y["km"], y["km_business"]) == (
        D("1000.00"),
        D("700.00"),
        D("550"),
        D("300"),
    )
    assert (y["business_cost"], y["business_cost_excl_financing"]) == (D("545.45"), D("381.82"))
    assert y["complete"] is False
    assert load_vollkosten(pool, "yearly", "multivan") == []


def test_vollkosten_costs_without_km_have_no_cost_per_km(pool: ConnectionPool, schema: TripStore) -> None:  # noqa: F811
    _cost(pool, "c1", "2026-03-01", "tax", "120.00", vehicle="multivan")
    _insert_trip(pool, "P", at("2026-03-02"), vehicle="multivan", gps_km="40", status="odometer_pending")
    (row,) = load_vollkosten(pool, "monthly", "multivan")
    assert row["cost_total"] == D("120.00") and row["km"] == 0
    assert row["cost_per_km"] is None and row["business_cost"] is None
    assert (row["trips_km_unknown"], row["km_gps_unconfirmed"], row["complete"]) == (1, D("40"), False)
