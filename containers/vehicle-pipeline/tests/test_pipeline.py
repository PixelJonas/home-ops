from typing import Any

import pytest

from vehicle_pipeline.extract import ExtractionResult
from vehicle_pipeline.pipeline import process_document

VEHICLES = {"WVGZZZE27SE017858": "id4"}


class FakePaperless:
    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        return self._doc

    async def get_tag_names(self, tag_ids: list[int]) -> dict[int, str]:
        return {tid: name for tid, name in zip(tag_ids, ["Fahrzeug:ID4-auto"], strict=False)}

    def document_url(self, doc_id: int) -> str:
        return f"https://paperless.example.test/documents/{doc_id}/"


class FakeExtractor:
    def __init__(self, result: ExtractionResult | Exception) -> None:
        self._result = result

    async def extract(self, text: str) -> ExtractionResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeReviewStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_draft(self, **kwargs: Any) -> int:
        self.created.append(kwargs)
        return len(self.created)


class FakeIdempotentReviewStore:
    """Mimics ReviewQueueStore.create_draft's pending-dedup behavior, to
    verify process_document's contract with the store without requiring a
    real Postgres connection. The actual correctness guarantee lives in
    ReviewQueueStore itself (see test_review_store_integration.py) — this
    only checks that pipeline.process_document doesn't route around it."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def create_draft(self, *, paperless_doc_id: int, **kwargs: Any) -> int:
        for idx, row in enumerate(self.rows):
            if row["paperless_doc_id"] == paperless_doc_id and row["status"] == "pending":
                return idx
        self.rows.append({"paperless_doc_id": paperless_doc_id, "status": "pending", **kwargs})
        return len(self.rows) - 1


@pytest.mark.asyncio
async def test_process_document_creates_review_draft() -> None:
    doc = {"id": 42, "title": "KFZ-Steuerbescheid 2026", "content": "...", "tags": [7]}
    paperless = FakePaperless(doc)
    extraction = ExtractionResult(category="kfz_steuer", amount=120.5, date="2026-03-01",
                                   vendor="Hauptzollamt", odometer_km=None, notes=None, confidence="high")
    llm = FakeExtractor(extraction)
    review_store = FakeReviewStore()

    await process_document(42, paperless, llm, review_store, VEHICLES)

    assert len(review_store.created) == 1
    draft = review_store.created[0]
    assert draft["mygarage_entity"] == "tax-records"
    assert draft["vin"] == "WVGZZZE27SE017858"
    assert draft["extracted_category"] == "kfz_steuer"


@pytest.mark.asyncio
async def test_process_document_with_no_vehicle_match_still_creates_generic_draft() -> None:
    doc = {"id": 43, "title": "Unklares Dokument", "content": "kein Fahrzeugbezug", "tags": []}
    paperless = FakePaperless(doc)
    extraction = ExtractionResult(category="other", amount=None, date=None, vendor=None,
                                   odometer_km=None, notes=None, confidence="low")
    llm = FakeExtractor(extraction)
    review_store = FakeReviewStore()

    await process_document(43, paperless, llm, review_store, VEHICLES)

    assert len(review_store.created) == 1
    assert review_store.created[0]["vin"] is None


@pytest.mark.asyncio
async def test_process_document_extraction_failure_still_creates_low_confidence_draft() -> None:
    from vehicle_pipeline.extract import ExtractionError

    doc = {"id": 44, "title": "Kaputtes Dokument", "content": "...", "tags": [7]}
    paperless = FakePaperless(doc)
    llm = FakeExtractor(ExtractionError("boom"))
    review_store = FakeReviewStore()

    await process_document(44, paperless, llm, review_store, VEHICLES)

    assert len(review_store.created) == 1
    draft = review_store.created[0]
    assert draft["extracted_category"] == "other"
    assert draft["confidence"] == "low"
    assert draft["mygarage_entity"] == "documents"


@pytest.mark.asyncio
async def test_process_document_double_trigger_does_not_duplicate_review_item() -> None:
    """Simulates a webhook delivery followed by a reconciliation pass
    re-scanning the same document (disjoint idempotency keyspaces at the
    ingest layer previously let both paths reach process_document for the
    same doc). process_document itself just calls create_draft each time —
    the dedup guarantee belongs to the store (see
    test_review_store_integration.py's idempotency tests), but this
    confirms process_document doesn't bypass that contract."""
    doc = {"id": 42, "title": "KFZ-Steuerbescheid 2026", "content": "...", "tags": [7]}
    paperless = FakePaperless(doc)
    extraction = ExtractionResult(category="kfz_steuer", amount=120.5, date="2026-03-01",
                                   vendor="Hauptzollamt", odometer_km=None, notes=None, confidence="high")
    review_store = FakeIdempotentReviewStore()

    await process_document(42, paperless, FakeExtractor(extraction), review_store, VEHICLES)
    await process_document(42, paperless, FakeExtractor(extraction), review_store, VEHICLES)

    assert len(review_store.rows) == 1
