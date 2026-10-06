"""Local cost sink against a real Postgres. Runs only when
INTEGRATION_DATABASE_URL is set -- use a throwaway database, these tests
create/delete their own rows (test VINs, review items, mygarage_seed rows)."""

import importlib.util
import os
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from seed_fixture import VIN_A, VIN_B, write_export
from vehicle_pipeline.config import VehicleConfig
from vehicle_pipeline.cost_store import PostgresCostStore, ReviewItemNotPendingError, UnknownVehicleError
from vehicle_pipeline.db import init_schema, upsert_vehicles
from vehicle_pipeline.review_store import ReviewQueueStore

DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")

DOC_ID = 7_770_001
VEHICLES = (
    VehicleConfig(slug="test-a", vin=VIN_A, label="Test A", make="VW", model="Multivan", fuel_type="diesel"),
    VehicleConfig(slug="test-b", vin=VIN_B, label="Test B", fuel_type="electric"),
)


@pytest.fixture
def pool():
    assert DATABASE_URL is not None
    p = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    init_schema(p)
    init_schema(p)  # must be idempotent: runs on every app start
    upsert_vehicles(p, VEHICLES)
    _clean(p)
    yield p
    _clean(p)
    p.close()


def _clean(p: ConnectionPool) -> None:
    with p.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM vehicle_pipeline.costs WHERE paperless_doc_id = %s OR source = 'mygarage_seed' "
            "AND vehicle_id IN ('test-a', 'test-b')",
            (DOC_ID,),
        )
        cur.execute("DELETE FROM vehicle_pipeline.review_items WHERE paperless_doc_id = %s", (DOC_ID,))


def _count(p: ConnectionPool, sql: str, params: tuple = ()) -> int:
    with p.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _draft(store: ReviewQueueStore, origin: str = "paperless") -> int:
    return store.create_draft(
        paperless_doc_id=DOC_ID, paperless_doc_title="Tankquittung", paperless_doc_url="https://p.test/d/1/",
        vin=VIN_A, mygarage_entity="cost", extracted_category="kraftstoff",
        payload={"vin": VIN_A, "category": "fuel", "date": "2026-09-01", "amount_gross": 65.4},
        confidence="high", origin=origin,
    )


def test_schema_and_vehicle_upsert(pool: ConnectionPool) -> None:
    upsert_vehicles(pool, (VehicleConfig(slug="test-a", vin=VIN_A, label="Renamed"),))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT label, active FROM vehicle_pipeline.vehicles WHERE id = 'test-a'")
        assert cur.fetchone() == ("Renamed", True)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'vehicle_pipeline' AND table_name = 'review_items'"
        )
        columns = {r[0] for r in cur.fetchall()}
    assert {"sink_record_id", "mygarage_record_id", "origin"} <= columns


def test_insert_from_review_is_atomic(pool: ConnectionPool) -> None:
    reviews = ReviewQueueStore(pool)
    costs = PostgresCostStore(pool)
    item_id = _draft(reviews, origin="ingestbuddy")
    item = reviews.get(item_id)
    assert item is not None

    payload = {**item.payload, "vin": "test-a", "odometer_km": "21714", "extra": {"k": "v"}}
    cost_id = costs.insert_from_review(item, payload)

    approved = reviews.get(item_id)
    assert approved is not None
    assert (approved.status, approved.sink_record_id, approved.mygarage_record_id) == ("approved", str(cost_id), None)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT vehicle_id, category::text, amount_gross::text, odometer_km, source, source_ref, "
            "review_item_id, extra FROM vehicle_pipeline.costs WHERE id = %s",
            (cost_id,),
        )
        assert cur.fetchone() == (
            "test-a", "fuel", "65.40", 21714, "ingestbuddy", f"review_item:{item_id}", item_id, {"k": "v"}
        )

    # approving the same (stale) item again must not create a second cost
    with pytest.raises(ReviewItemNotPendingError):
        costs.insert_from_review(item, payload)
    assert _count(pool, "SELECT count(*) FROM vehicle_pipeline.costs WHERE paperless_doc_id = %s", (DOC_ID,)) == 1


def test_unknown_vehicle_leaves_item_pending(pool: ConnectionPool) -> None:
    reviews = ReviewQueueStore(pool)
    item_id = _draft(reviews)
    item = reviews.get(item_id)
    assert item is not None

    with pytest.raises(UnknownVehicleError):
        PostgresCostStore(pool).insert_from_review(item, {**item.payload, "vin": "NOSUCHVIN00000000"})

    after = reviews.get(item_id)
    assert after is not None and after.status == "pending" and after.sink_record_id is None
    assert _count(pool, "SELECT count(*) FROM vehicle_pipeline.costs WHERE paperless_doc_id = %s", (DOC_ID,)) == 0


def test_seed_sql_applies_twice_with_identical_counts(pool: ConnectionPool, tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "gen_mygarage_seed.py"
    spec = importlib.util.spec_from_file_location("gen_mygarage_seed", script)
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    sql = gen.render_sql(gen.build_rows(write_export(tmp_path)))
    sql = "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))  # psql meta-commands

    counts = []
    for _ in range(2):
        with pool.connection() as conn:
            conn.autocommit = True
            conn.execute(sql)
            conn.autocommit = False
        counts.append(_count(pool, "SELECT count(*) FROM vehicle_pipeline.costs WHERE source = 'mygarage_seed'"))
    assert counts == [6, 6]
    assert _count(
        pool, "SELECT count(*) FROM vehicle_pipeline.costs WHERE source = 'mygarage_seed' AND category = 'charging'"
    ) == 1
