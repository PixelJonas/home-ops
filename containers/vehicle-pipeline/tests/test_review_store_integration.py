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


def test_create_draft_is_idempotent_while_pending() -> None:
    """Simulates a webhook delivery followed by a reconciliation pass
    re-scanning the same document: create_draft must not create a second
    review_items row while the first draft is still pending, regardless of
    which caller (webhook vs. reconciliation) invokes it."""
    assert DATABASE_URL is not None
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    init_schema(pool)
    store = ReviewQueueStore(pool)
    doc_id = 8675310

    try:
        _clean(pool, doc_id)

        first_id = store.create_draft(
            paperless_doc_id=doc_id,
            paperless_doc_title="KFZ-Steuer Bescheid 2026 (webhook)",
            paperless_doc_url="https://paperless.example.test/documents/8675310/",
            vin="WVGZZZE27SE017858",
            mygarage_entity="tax-records",
            extracted_category="kfz_steuer",
            payload={"date": "2026-03-01", "amount": 120.0, "vin": "WVGZZZE27SE017858"},
            confidence="high",
        )
        second_id = store.create_draft(
            paperless_doc_id=doc_id,
            paperless_doc_title="KFZ-Steuer Bescheid 2026 (reconciliation)",
            paperless_doc_url="https://paperless.example.test/documents/8675310/",
            vin="WVGZZZE27SE017858",
            mygarage_entity="tax-records",
            extracted_category="kfz_steuer",
            payload={"date": "2026-03-01", "amount": 120.0, "vin": "WVGZZZE27SE017858"},
            confidence="high",
        )

        assert first_id == second_id
        pending_for_doc = [item for item in store.list_pending() if item.paperless_doc_id == doc_id]
        assert len(pending_for_doc) == 1

        # After the pending draft is resolved, a subsequent create_draft for
        # the same doc_id must create a genuinely new row (e.g. a future
        # "Document Updated" trigger re-drafting an already-handled doc).
        store.mark_approved(first_id, mygarage_record_id="99")
        third_id = store.create_draft(
            paperless_doc_id=doc_id,
            paperless_doc_title="KFZ-Steuer Bescheid 2026 (re-draft after approval)",
            paperless_doc_url="https://paperless.example.test/documents/8675310/",
            vin="WVGZZZE27SE017858",
            mygarage_entity="tax-records",
            extracted_category="kfz_steuer",
            payload={"date": "2026-03-01", "amount": 125.0, "vin": "WVGZZZE27SE017858"},
            confidence="high",
        )
        assert third_id != first_id
    finally:
        _clean(pool, doc_id)
        pool.close()


def test_create_draft_after_rejection_creates_new_row() -> None:
    assert DATABASE_URL is not None
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    init_schema(pool)
    store = ReviewQueueStore(pool)
    doc_id = 8675311

    try:
        _clean(pool, doc_id)

        first_id = store.create_draft(
            paperless_doc_id=doc_id,
            paperless_doc_title="Werkstattrechnung",
            paperless_doc_url="https://paperless.example.test/documents/8675311/",
            vin="WVGZZZE27SE017858",
            mygarage_entity="service-visits",
            extracted_category="hu_au",
            payload={"date": "2026-03-01", "total_cost": 200.0},
            confidence="medium",
        )
        store.mark_rejected(first_id)

        second_id = store.create_draft(
            paperless_doc_id=doc_id,
            paperless_doc_title="Werkstattrechnung",
            paperless_doc_url="https://paperless.example.test/documents/8675311/",
            vin="WVGZZZE27SE017858",
            mygarage_entity="service-visits",
            extracted_category="hu_au",
            payload={"date": "2026-03-01", "total_cost": 200.0},
            confidence="medium",
        )

        assert second_id != first_id
    finally:
        _clean(pool, doc_id)
        pool.close()
