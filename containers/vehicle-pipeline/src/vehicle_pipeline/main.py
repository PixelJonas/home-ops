from __future__ import annotations

import logging

from fastapi import FastAPI

from vehicle_pipeline.webhooks import router as webhooks_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="vehicle-pipeline")
app.include_router(webhooks_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "vehicle-pipeline"}
