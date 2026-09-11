import os

import pytest
from psycopg_pool import ConnectionPool

from vehicle_pipeline.db import init_schema
from vehicle_pipeline.review_store import ReviewQueueStore

DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")


def _clean(pool: ConnectionPool, doc_id: int) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM vehicle_pipeline.review_items WHERE paperless_doc_id = %s", (doc_id,))


def test_create_list_approve_roundtrip() -> None:
    assert DATABASE_URL is not None
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    init_schema(pool)
    store = ReviewQueueStore(pool)
    doc_id = 8675309

    try:
        _clean(pool, doc_id)
        item_id = store.create_draft(
            paperless_doc_id=doc_id,
            paperless_doc_title="KFZ-Steuer Bescheid 2026",
            paperless_doc_url="https://paperless.example.test/documents/8675309/",
            vin="WVGZZZE27SE017858",
            mygarage_entity="tax-records",
            extracted_category="kfz_steuer",
            payload={"date": "2026-03-01", "amount": 120.0, "vin": "WVGZZZE27SE017858"},
            confidence="high",
        )

        pending = store.list_pending()
        assert any(item.id == item_id and item.status == "pending" for item in pending)

        fetched = store.get(item_id)
        assert fetched is not None
        assert fetched.payload["amount"] == 120.0

        store.update_payload(item_id, {**fetched.payload, "amount": 130.0})
        store.mark_approved(item_id, mygarage_record_id="42")

        approved = store.get(item_id)
        assert approved is not None
        assert approved.status == "approved"
        assert approved.mygarage_record_id == "42"
        assert approved.payload["amount"] == 130.0
        assert not any(item.id == item_id for item in store.list_pending())
    finally:
        _clean(pool, doc_id)
        pool.close()
