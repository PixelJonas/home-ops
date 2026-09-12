from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from vehicle_pipeline.pipeline import process_document

logger = logging.getLogger("vehicle_pipeline.reconciliation")

LOOKBACK = timedelta(hours=1)
VEHICLE_TAGS = ["Fahrzeug:ID4-auto", "Fahrzeug:Multivan-auto"]


class WatermarkStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, iso: str) -> None: ...


async def run_reconciliation_pass(
    *,
    paperless: Any,
    ingest_store: Any,
    llm: Any,
    review_store: Any,
    vehicles: dict[str, str],
    watermark_store: WatermarkStore,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)

    tag_ids = [tid for name in VEHICLE_TAGS if (tid := await paperless.get_tag_id(name)) is not None]
    if not tag_ids:
        logger.warning("reconciliation skipped: no vehicle tags found in Paperless yet")
        return 0

    # LOOKBACK is applied unconditionally, not just on first run: Paperless may
    # have read-after-write lag between a document's `modified` timestamp and
    # that document becoming visible to the modified_since query, so a
    # steady-state watermark with no backward margin can let a document
    # permanently fall through the gap. The resulting overlap re-scan is cheap
    # because the reconciliation idempotency key includes `modified`
    # (`paperless-reconciliation:{doc_id}:{modified}`) — already-processed
    # documents in the overlap window are skipped via ingest_store.record_event
    # returning False, so nothing is double-processed. Matches the proven
    # taxbuddy ingestion_api reconciliation pattern.
    stored_watermark = watermark_store.get()
    since = (
        datetime.fromisoformat(stored_watermark) - LOOKBACK
        if stored_watermark
        else now - LOOKBACK
    )

    docs = await paperless.list_documents_by_tags_modified_since(tag_ids, since.isoformat())

    enqueued = 0
    for doc in docs:
        doc_id = int(doc["id"])
        modified = doc.get("modified", "")
        event_id = f"paperless-reconciliation:{doc_id}:{modified}"
        if ingest_store.record_event(event_id=event_id, source="paperless-reconciliation", payload=doc):
            await process_document(doc_id, paperless, llm, review_store, vehicles)
            enqueued += 1

    watermark_store.set(now.isoformat())
    return enqueued


async def reconciliation_loop(
    *,
    interval_seconds: int,
    paperless: Any,
    ingest_store: Any,
    llm: Any,
    review_store: Any,
    vehicles: dict[str, str],
    watermark_store: WatermarkStore,
) -> None:
    while True:
        try:
            enqueued = await run_reconciliation_pass(
                paperless=paperless,
                ingest_store=ingest_store,
                llm=llm,
                review_store=review_store,
                vehicles=vehicles,
                watermark_store=watermark_store,
            )
            if enqueued:
                logger.info("reconciliation enqueued %d document(s)", enqueued)
        except Exception:
            logger.exception("reconciliation pass failed")
        await asyncio.sleep(interval_seconds)
