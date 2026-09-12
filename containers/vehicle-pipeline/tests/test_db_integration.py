"""Runs only when INTEGRATION_DATABASE_URL is set to a real Postgres with
init_schema() already applied — see Task 13 for port-forwarding to the
live CNPG cluster."""

import os

import pytest
from psycopg_pool import ConnectionPool

from vehicle_pipeline.db import PostgresIngestEventStore, init_schema

DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")


def test_record_event_inserts_once_and_dedupes_after() -> None:
    assert DATABASE_URL is not None
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    init_schema(pool)
    store = PostgresIngestEventStore(pool)
    event_id = "paperless:test-t3-db-integration"

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM vehicle_pipeline.ingest_events WHERE event_id = %s", (event_id,))

        first = store.record_event(event_id, "paperless", {"doc_id": 999999})
        second = store.record_event(event_id, "paperless", {"doc_id": 999999})

        assert first is True
        assert second is False
    finally:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM vehicle_pipeline.ingest_events WHERE event_id = %s", (event_id,))
        pool.close()
