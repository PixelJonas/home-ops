from __future__ import annotations

import logging
from typing import Any, Protocol

from vehicle_pipeline.classify_vehicle import classify_vehicle
from vehicle_pipeline.extract import ExtractionError, ExtractionResult
from vehicle_pipeline.taxonomy import map_to_mygarage

logger = logging.getLogger("vehicle_pipeline.pipeline")


class PaperlessProtocol(Protocol):
    async def get_document(self, doc_id: int) -> dict[str, Any]: ...
    async def get_tag_names(self, tag_ids: list[int]) -> dict[int, str]: ...
    def document_url(self, doc_id: int) -> str: ...


class LLMProtocol(Protocol):
    async def extract(self, document_text: str) -> ExtractionResult: ...


class ReviewStoreProtocol(Protocol):
    def create_draft(self, **kwargs: Any) -> int: ...


async def process_document(
    doc_id: int,
    paperless: PaperlessProtocol,
    llm: LLMProtocol,
    review_store: ReviewStoreProtocol,
    vehicles: dict[str, str],
) -> None:
    doc = await paperless.get_document(doc_id)
    tag_names = list((await paperless.get_tag_names(doc.get("tags", []))).values())
    content = doc.get("content", "")
    vin = classify_vehicle(tag_names, content, vehicles)

    try:
        extraction = await llm.extract(content)
    except ExtractionError:
        logger.exception("extraction failed for doc_id=%s, routing to generic review", doc_id)
        extraction = ExtractionResult(
            category="other", amount=None, date=None, vendor=None,
            odometer_km=None, notes="LLM extraction failed — manual review required",
            confidence="low",
        )

    draft = map_to_mygarage(extraction, vin or "")

    review_store.create_draft(
        paperless_doc_id=doc_id,
        paperless_doc_title=doc.get("title", f"Document {doc_id}"),
        paperless_doc_url=paperless.document_url(doc_id),
        vin=vin,
        mygarage_entity=draft.entity,
        extracted_category=extraction.category,
        payload=draft.payload,
        confidence=extraction.confidence,
    )
