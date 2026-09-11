from __future__ import annotations

from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
"""


def init_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA_DDL)


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
