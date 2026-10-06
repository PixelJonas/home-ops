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

-- Business/private notifications (trips.notify). notify_id is the short,
-- action-ID-safe handle used in TRIP_BIZ_<id>/TRIP_PRIV_<id>; trip keys
-- contain ':' and are not guaranteed short.
CREATE TABLE IF NOT EXISTS trips.notifications_sent (
    trip_key TEXT PRIMARY KEY,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE trips.notifications_sent ADD COLUMN IF NOT EXISTS notify_id TEXT;
ALTER TABLE trips.notifications_sent ADD COLUMN IF NOT EXISTS target TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS notifications_sent_notify_id_idx
    ON trips.notifications_sent (notify_id);

-- Notification cutoff: trips that ended before it are never notified. The
-- row is written once, on the first notify-enabled detector start, so the
-- backfilled history never triggers a burst of notifications.
CREATE TABLE IF NOT EXISTS trips.notify_state (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    cutoff TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Imported in-car trip memories (trips.vw_export / trips.imports): raw,
-- append-only. One row per export file set; one row per distinct exported
-- row (row_hash, so re-importing the same or a newer overlapping export
-- adds nothing twice) linked to every export that contained it. The VIN is
-- never stored (mapped to vehicle_id on import).
CREATE TABLE IF NOT EXISTS trips.import_exports (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('vw_export')),
    vehicle_id TEXT NOT NULL,
    export_created_at TIMESTAMPTZ NOT NULL,
    export_odometer_km NUMERIC,
    engine TEXT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, vehicle_id, export_created_at)
);

CREATE TABLE IF NOT EXISTS trips.imported_trips (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('vw_export')),
    vehicle_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('short_term', 'refuel_segment', 'long_term')),
    ended_at TIMESTAMPTZ NOT NULL,
    km NUMERIC NOT NULL,
    driving_time INTERVAL,
    avg_speed_kmh NUMERIC,
    avg_consumption NUMERIC,
    consumption_unit TEXT,
    total_consumption NUMERIC,
    total_consumption_unit TEXT,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- the export this row was first seen in
    export_created_at TIMESTAMPTZ NOT NULL,
    export_odometer_km NUMERIC,
    row_hash TEXT NOT NULL UNIQUE,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS imported_trips_vehicle_kind_idx
    ON trips.imported_trips (vehicle_id, kind, ended_at);

CREATE TABLE IF NOT EXISTS trips.imported_trip_exports (
    imported_trip_id BIGINT NOT NULL REFERENCES trips.imported_trips (id),
    export_id BIGINT NOT NULL REFERENCES trips.import_exports (id),
    PRIMARY KEY (imported_trip_id, export_id)
);

-- The read side: the trips projection with the effective (latest per
-- field) annotation applied. A JSON null annotation clears the field.
-- Driver/vehicle overrides replace the detected values here; the detected
-- ones stay visible as detected_*. An imported trip inherits the
-- annotations of the detector trips it superseded (evidence.supersedes);
-- its own annotations win over inherited ones.
CREATE OR REPLACE VIEW trips.trips_effective AS
WITH ann_keys AS (
    SELECT trip_key, trip_key AS ann_key, 0 AS prio FROM trips.trips
    UNION ALL
    SELECT t.trip_key, s.key, 1
    FROM trips.trips t
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(t.evidence -> 'supersedes') = 'array' THEN t.evidence -> 'supersedes' ELSE '[]'::jsonb END
    ) AS s(key)
),
latest AS (
    SELECT DISTINCT ON (k.trip_key, a.field) k.trip_key, a.field, a.value, a.actor, a.via, a.created_at
    FROM ann_keys k
    JOIN trips.trip_annotations a ON a.trip_key = k.ann_key
    ORDER BY k.trip_key, a.field, k.prio, a.created_at DESC, a.id DESC
)
SELECT
    t.trip_key,
    COALESCE(v.value #>> '{}', t.vehicle_id) AS vehicle_id,
    COALESCE(d.value #>> '{}', t.driver) AS driver,
    t.started_at,
    t.ended_at,
    t.odo_start,
    t.odo_end,
    t.km,
    t.gps_km,
    t.status,
    t.source,
    CASE WHEN jsonb_typeof(b.value) = 'boolean' THEN b.value::boolean END AS business,
    b.via AS business_via,
    b.actor AS business_actor,
    b.created_at AS business_at,
    p.value #>> '{}' AS purpose,
    t.vehicle_id AS detected_vehicle_id,
    t.driver AS detected_driver,
    t.evidence,
    t.updated_at
FROM trips.trips t
LEFT JOIN latest b ON b.trip_key = t.trip_key AND b.field = 'business'
LEFT JOIN latest p ON p.trip_key = t.trip_key AND p.field = 'purpose'
LEFT JOIN latest d ON d.trip_key = t.trip_key AND d.field = 'driver'
LEFT JOIN latest v ON v.trip_key = t.trip_key AND v.field = 'vehicle';
"""


def init_trips_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
            cur.execute(TRIPS_DDL)
