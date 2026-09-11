from __future__ import annotations

import logging

from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="vehicle-pipeline")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "vehicle-pipeline"}
