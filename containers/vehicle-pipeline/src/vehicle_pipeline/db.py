from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from vehicle_pipeline.config import VehicleConfig

_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS vehicle_pipeline;

CREATE TABLE IF NOT EXISTS vehicle_pipeline.ingest_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vehicle_pipeline.review_items (
    id BIGSERIAL PRIMARY KEY,
    paperless_doc_id INTEGER NOT NULL,
    paperless_doc_title TEXT NOT NULL,
    paperless_doc_url TEXT NOT NULL,
    vin TEXT,
    mygarage_entity TEXT NOT NULL,
    extracted_category TEXT NOT NULL,
    payload JSONB NOT NULL,
    confidence TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    mygarage_record_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS vehicle_pipeline.reconciliation_watermark (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_run_at TIMESTAMPTZ
);
INSERT INTO vehicle_pipeline.reconciliation_watermark (id, last_run_at)
VALUES (1, NULL) ON CONFLICT (id) DO NOTHING;

-- Local cost sink (Vollkostenrechnung P1): replaces MyGarage as the
-- system of record for vehicle costs. Everything below is additive and
-- idempotent so it is safe on every startup against an existing DB.
ALTER TABLE vehicle_pipeline.review_items ADD COLUMN IF NOT EXISTS sink_record_id TEXT;
ALTER TABLE vehicle_pipeline.review_items ADD COLUMN IF NOT EXISTS origin TEXT;

CREATE TABLE IF NOT EXISTS vehicle_pipeline.vehicles (
    id TEXT PRIMARY KEY,
    vin TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    make TEXT,
    model TEXT,
    fuel_type TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    CREATE TYPE vehicle_pipeline.cost_category AS ENUM (
        'fuel', 'charging', 'service', 'insurance', 'tax',
        'financing', 'def', 'parking', 'toll', 'other'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS vehicle_pipeline.costs (
    id BIGSERIAL PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicle_pipeline.vehicles (id),
    date DATE NOT NULL,
    category vehicle_pipeline.cost_category NOT NULL,
    amount_gross NUMERIC(12, 2) NOT NULL,
    amount_net NUMERIC(12, 2),
    vat_amount NUMERIC(12, 2),
    vat_rate NUMERIC(5, 2),
    currency TEXT NOT NULL DEFAULT 'EUR',
    odometer_km INTEGER,
    quantity_liters NUMERIC,
    quantity_kwh NUMERIC,
    vendor TEXT,
    paperless_doc_id INTEGER,
    review_item_id BIGINT UNIQUE REFERENCES vehicle_pipeline.review_items (id),
    source TEXT NOT NULL CHECK (source IN ('mygarage_seed', 'paperless', 'ingestbuddy', 'manual')),
    source_ref TEXT,
    extra JSONB,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_ref)
);
CREATE INDEX IF NOT EXISTS costs_vehicle_date_idx ON vehicle_pipeline.costs (vehicle_id, date);
"""


def init_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA_DDL)


def upsert_vehicles(pool: ConnectionPool, vehicles: Iterable[VehicleConfig]) -> None:
    """Sync the configured vehicle registry into vehicle_pipeline.vehicles.
    Never deletes or deactivates rows: a vehicle dropped from config keeps
    its history (flip `active` by hand)."""
    with pool.connection() as conn, conn.cursor() as cur:
        for v in vehicles:
            cur.execute(
                """
                INSERT INTO vehicle_pipeline.vehicles (id, vin, label, make, model, fuel_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    vin = EXCLUDED.vin, label = EXCLUDED.label, make = EXCLUDED.make,
                    model = EXCLUDED.model, fuel_type = EXCLUDED.fuel_type
                """,
                (v.slug, v.vin, v.label, v.make, v.model, v.fuel_type),
            )


class IngestEventStore(Protocol):
    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        """Insert the event if new. True the first time event_id is seen,
        False if it already existed (caller should treat as duplicate)."""
        ...


class PostgresIngestEventStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vehicle_pipeline.ingest_events (event_id, source, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, source, Jsonb(payload)),
            )
            return cur.rowcount > 0


class PostgresWatermarkStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def get(self) -> str | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT last_run_at FROM vehicle_pipeline.reconciliation_watermark WHERE id = 1")
            row = cur.fetchone()
            return row[0].isoformat() if row and row[0] else None

    def set(self, iso: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicle_pipeline.reconciliation_watermark SET last_run_at = %s WHERE id = 1",
                (iso,),
            )
