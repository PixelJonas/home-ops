from typing import Any

import pytest

from vehicle_pipeline.reconciliation import run_reconciliation_pass


class FakePaperless:
    def __init__(self, tag_ids: dict[str, int], docs: list[dict[str, Any]]) -> None:
        self._tag_ids = tag_ids
        self._docs = docs
        self.queried_since: str | None = None

    async def get_tag_id(self, name: str) -> int | None:
        return self._tag_ids.get(name)

    async def list_documents_by_tags_modified_since(self, tag_ids: list[int], since_iso: str) -> list[dict[str, Any]]:
        self.queried_since = since_iso
        return self._docs

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        return {"id": doc_id, "title": f"doc {doc_id}", "content": "", "tags": []}

    async def get_tag_names(self, tag_ids: list[int]) -> dict[int, str]:
        return {}

    def document_url(self, doc_id: int) -> str:
        return f"https://paperless.example.test/documents/{doc_id}/"


class FakeIngestStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        if event_id in self.seen:
            return False
        self.seen.add(event_id)
        return True


class FakeWatermarkStore:
    def __init__(self, initial: str | None = None) -> None:
        self._value = initial

    def get(self) -> str | None:
        return self._value

    def set(self, iso: str) -> None:
        self._value = iso


class FakeExtractorAlwaysOther:
    async def extract(self, text: str):
        from vehicle_pipeline.extract import ExtractionResult
        return ExtractionResult(category="other", amount=None, date=None, vendor=None,
                                 odometer_km=None, notes=None, confidence="low")


class FakeReviewStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_draft(self, **kwargs: Any) -> int:
        self.created.append(kwargs)
        return len(self.created)


@pytest.mark.asyncio
async def test_reconciliation_enqueues_new_documents() -> None:
    paperless = FakePaperless(
        {"Fahrzeug:ID4-auto": 7, "Fahrzeug:Multivan-auto": 9},
        docs=[{"id": 100, "modified": "2026-09-10T12:00:00+00:00"}],
    )
    ingest_store = FakeIngestStore()
    watermark = FakeWatermarkStore(initial="2026-09-01T00:00:00+00:00")

    enqueued = await run_reconciliation_pass(
        paperless=paperless,
        ingest_store=ingest_store,
        llm=FakeExtractorAlwaysOther(),
        review_store=FakeReviewStore(),
        vehicles={"WVGZZZE27SE017858": "id4"},
        watermark_store=watermark,
    )

    assert enqueued == 1
    assert paperless.queried_since == "2026-09-01T00:00:00+00:00"
    assert watermark.get() is not None


@pytest.mark.asyncio
async def test_reconciliation_skips_when_tags_not_found_yet() -> None:
    paperless = FakePaperless({}, docs=[])
    enqueued = await run_reconciliation_pass(
        paperless=paperless,
        ingest_store=FakeIngestStore(),
        llm=FakeExtractorAlwaysOther(),
        review_store=FakeReviewStore(),
        vehicles={},
        watermark_store=FakeWatermarkStore(),
    )
    assert enqueued == 0


@pytest.mark.asyncio
async def test_reconciliation_dedupes_against_already_seen_events() -> None:
    paperless = FakePaperless(
        {"Fahrzeug:ID4-auto": 7, "Fahrzeug:Multivan-auto": 9},
        docs=[{"id": 100, "modified": "2026-09-10T12:00:00+00:00"}],
    )
    ingest_store = FakeIngestStore()
    ingest_store.seen.add("paperless-reconciliation:100:2026-09-10T12:00:00+00:00")

    enqueued = await run_reconciliation_pass(
        paperless=paperless,
        ingest_store=ingest_store,
        llm=FakeExtractorAlwaysOther(),
        review_store=FakeReviewStore(),
        vehicles={},
        watermark_store=FakeWatermarkStore(initial="2026-09-01T00:00:00+00:00"),
    )
    assert enqueued == 0
