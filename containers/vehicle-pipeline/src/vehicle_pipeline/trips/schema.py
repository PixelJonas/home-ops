"""DDL for the ``trips`` Postgres schema (same database as
vehicle_pipeline). Idempotent; run by the detector at startup inside a
transaction holding SCHEMA_LOCK_KEY, the same advisory lock
vehicle_pipeline.db.init_schema takes, so concurrent starts of the app and
one or more detectors never race on catalog inserts.

Raw tables (odometer_readings, drive_spans, trip_positions,
trip_annotations) are the record; ``trips`` is a projection recomputed from
them and may be truncated and rebuilt at any time.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

# Arbitrary constant shared with vehicle_pipeline.db.init_schema.
SCHEMA_LOCK_KEY = 7_365_802_211

TRIPS_DDL = """
CREATE SCHEMA IF NOT EXISTS trips;

CREATE TABLE IF NOT EXISTS trips.odometer_readings (
    id BIGSERIAL PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    km NUMERIC NOT NULL,
    data_captured_at TIMESTAMPTZ NOT NULL,
    ha_last_changed TIMESTAMPTZ,
    source TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vehicle_id, data_captured_at, km, source)
);
CREATE INDEX IF NOT EXISTS odometer_readings_vehicle_captured_idx
    ON trips.odometer_readings (vehicle_id, data_captured_at);

CREATE TABLE IF NOT EXISTS trips.drive_spans (
    id BIGSERIAL PRIMARY KEY,
    person TEXT NOT NULL,
    vehicle_id TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    source TEXT NOT NULL DEFAULT 'ha_phone',
    detector_version INTEGER NOT NULL,
    positions_fetched_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person, started_at, source)
);
CREATE INDEX IF NOT EXISTS drive_spans_started_idx ON trips.drive_spans (started_at);

CREATE TABLE IF NOT EXISTS trips.trip_positions (
    person TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    speed_kmh NUMERIC,
    PRIMARY KEY (person, ts)
);

CREATE TABLE IF NOT EXISTS trips.trip_annotations (
    id BIGSERIAL PRIMARY KEY,
    trip_key TEXT NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('business', 'purpose', 'driver', 'vehicle')),
    value JSONB NOT NULL,
    actor TEXT,
    via TEXT NOT NULL CHECK (via IN ('notify', 'ui', 'api', 'import')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trip_annotations_key_idx
    ON trips.trip_annotations (trip_key, field, created_at DESC);

CREATE TABLE IF NOT EXISTS trips.trips (
    trip_key TEXT PRIMARY KEY,
    vehicle_id TEXT,
    driver TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    odo_start NUMERIC,
    odo_end NUMERIC,
    km NUMERIC,
    gps_km NUMERIC,
    status TEXT NOT NULL CHECK (status IN ('ok', 'odometer_split', 'odometer_pending', 'odometer_stale')),
    source TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trips_vehicle_started_idx ON trips.trips (vehicle_id, started_at);

CREATE TABLE IF NOT EXISTS trips.trip_projection_log (
    id BIGSERIAL PRIMARY KEY,
    trip_key TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    old JSONB,
    new JSONB,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trip_projection_log_key_idx ON trips.trip_projection_log (trip_key, changed_at);

CREATE TABLE IF NOT EXISTS trips.cursors (
    entity_id TEXT PRIMARY KEY,
    last_ts TIMESTAMPTZ NOT NULL
);

-- Used by the (later) notification phase; created now so it never needs a
-- migration of its own.
CREATE TABLE IF NOT EXISTS trips.notifications_sent (
    trip_key TEXT PRIMARY KEY,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_trips_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
            cur.execute(TRIPS_DDL)
