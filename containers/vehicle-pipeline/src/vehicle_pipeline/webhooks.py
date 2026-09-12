from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from vehicle_pipeline.config import Settings
from vehicle_pipeline.db import IngestEventStore
from vehicle_pipeline.pipeline import process_document

logger = logging.getLogger("vehicle_pipeline.webhooks")

router = APIRouter()


class PaperlessWebhookPayload(BaseModel):
    doc_id: int
    doc_title: str
    doc_url: str
    correspondent: str
    document_type: str
    added: str
    owner_username: str


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_ingest_store(request: Request) -> IngestEventStore:
    return request.app.state.ingest_store  # type: ignore[no-any-return]


def get_review_store(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.review_store


def get_paperless(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.paperless


def get_llm(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.llm


async def _process_document_background(
    doc_id: int, paperless, llm, review_store, vehicles: dict[str, str]  # type: ignore[no-untyped-def]
) -> None:
    """BackgroundTasks wrapper around process_document.

    Starlette re-raises exceptions from background tasks through the ASGI
    call stack that already sent the response — an unhandled exception here
    would surface as a broken request even though the client already got its
    200. Catch and log instead so one bad document never takes down webhook
    delivery for the rest.
    """
    try:
        await process_document(doc_id, paperless, llm, review_store, vehicles)
    except Exception:
        logger.exception("background processing failed for doc_id=%s", doc_id)


@router.post("/webhooks/paperless-vehicle")
async def receive_paperless_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),  # noqa: B008
    ingest_store: IngestEventStore = Depends(get_ingest_store),  # noqa: B008
    review_store=Depends(get_review_store),  # noqa: B008
    paperless=Depends(get_paperless),  # noqa: B008
    llm=Depends(get_llm),  # noqa: B008
) -> JSONResponse:
    provided_signature = request.headers.get("x-vehicle-pipeline-signature", "")
    if not hmac.compare_digest(provided_signature, settings.paperless_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    try:
        webhook = PaperlessWebhookPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc

    inserted = ingest_store.record_event(
        event_id=f"paperless:{webhook.doc_id}", source="paperless", payload=payload
    )
    if not inserted:
        return JSONResponse({"status": "duplicate"})

    background_tasks.add_task(
        _process_document_background, webhook.doc_id, paperless, llm, review_store, settings.vehicles
    )
    logger.info("queued document %s for processing", webhook.doc_id)
    return JSONResponse({"status": "queued"})
