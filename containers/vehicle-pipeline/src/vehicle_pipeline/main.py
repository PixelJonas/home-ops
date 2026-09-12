# src/vehicle_pipeline/main.py
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from vehicle_pipeline.config import Settings
from vehicle_pipeline.db import PostgresIngestEventStore, PostgresWatermarkStore, init_schema
from vehicle_pipeline.extract import LiteLLMExtractor
from vehicle_pipeline.mygarage_client import MyGarageClient
from vehicle_pipeline.paperless_client import PaperlessClient
from vehicle_pipeline.reconciliation import reconciliation_loop
from vehicle_pipeline.review_store import ReviewQueueStore
from vehicle_pipeline.review_ui import router as review_ui_router
from vehicle_pipeline.webhooks import router as webhooks_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("vehicle_pipeline")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=5, open=True)
    init_schema(pool)

    paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
    llm = LiteLLMExtractor(settings.litellm_base_url, settings.litellm_api_key)
    mygarage = MyGarageClient(settings.mygarage_url, settings.mygarage_username, settings.mygarage_password)

    app.state.settings = settings
    app.state.ingest_store = PostgresIngestEventStore(pool)
    app.state.review_store = ReviewQueueStore(pool)
    app.state.paperless = paperless
    app.state.llm = llm
    app.state.mygarage = mygarage

    poll_task = asyncio.create_task(
        reconciliation_loop(
            interval_seconds=settings.poll_interval_seconds,
            paperless=paperless,
            ingest_store=app.state.ingest_store,
            llm=llm,
            review_store=app.state.review_store,
            vehicles=settings.vehicles,
            watermark_store=PostgresWatermarkStore(pool),
        )
    )

    try:
        yield
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        for closer in (paperless.aclose, llm.aclose, mygarage.aclose):
            try:
                await closer()
            except Exception:
                logger.exception("error closing client during shutdown")
        pool.close()


app = FastAPI(title="vehicle-pipeline", lifespan=lifespan)
app.include_router(webhooks_router)
app.include_router(review_ui_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "vehicle-pipeline"}
