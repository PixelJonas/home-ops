# Vehicle Cost Pipeline (Gitea #16) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new home-ops service that watches Paperless for new/updated vehicle documents (Multivan + ID.4), extracts cost data via the shared LiteLLM gateway, and lands **drafts only** in a review queue that Jonas approves before anything is written to MyGarage.

**Architecture:** A single FastAPI service (`vehicle-pipeline`) in `containers/vehicle-pipeline/`, deployed as its own home-ops Application with a dedicated CNPG Postgres instance (idempotency ledger + review-queue table, no Redis/queue broker — low volume, so Postgres itself is the queue). Paperless calls a webhook on new/updated tagged documents; a periodic poll reconciles anything the webhook missed. Each triggered document is fetched from Paperless (already-OCR'd `content`), classified to a vehicle, extracted by one LiteLLM chat-completion call into a fixed JSON schema, mapped through a taxonomy table to a MyGarage entity type + payload, and inserted as a `pending` review-queue row. A basic-auth-gated `/review` page lets Jonas approve (commits to MyGarage via its REST API) or reject each draft.

**Tech Stack:** Python 3.12, FastAPI + uvicorn, psycopg3 + psycopg_pool (Postgres), httpx (Paperless/LiteLLM/MyGarage HTTP clients), pydantic v2, Jinja2 (server-rendered review UI), pytest. Mirrors `~/projects/taxbuddy/services/ingestion_api` and `services/doc_pipeline`'s code shape (FastAPI app + `stages`-style pure functions + a Postgres store class), simplified to one process/one repo per ticket #16's explicit "or simpler single service" allowance — no Redis, no vision-model page rasterization (Paperless's own OCR'd `content` text is extracted from directly, per ticket #16's "OCR text in → structured JSON out" spec), no fewshot-prompt infrastructure.

**Spec:** Gitea `git.janz.digital/jonas/infra-ops` issue #16 (comment 160) under map #5; taxonomy/category table and non-goals are decided there — this plan does not re-derive them. Related, already-decided tickets #17/#19 (backfills, extend this pipeline later), #38 (independent Zappi ingestion, not built here), #44 (blocked on external MyGarage fork, not built here).

## Global Constraints

- **Never auto-write to MyGarage.** Every extracted record becomes a `pending` `review_items` row; only `POST /review/{id}/approve` calls MyGarage's write API. No code path skips the review queue.
- Doppler project `homelab`, config `home` for all secrets (`VEHICLE_PIPELINE_*` keys) — use CLI with output suppression per this repo's CLAUDE.md, never MCP `secrets_update`/`secrets_get`.
- ArgoCD `ClusterSecretStore` for all app secrets is `doppler-cluster` (not `doppler-infra`).
- Cluster: `local.home` (Altus) overlay only — same cluster as `mygarage`/`taxbuddy` (`bootstrap/overlays/local.home/values-apps.yaml`).
- New Doppler keys needed (create in Task 15, referenced from Task 13 onward): `VEHICLE_PIPELINE_PAPERLESS_URL`, `VEHICLE_PIPELINE_PAPERLESS_TOKEN`, `VEHICLE_PIPELINE_PAPERLESS_WEBHOOK_SECRET`, `VEHICLE_PIPELINE_LITELLM_BASE_URL`, `VEHICLE_PIPELINE_LITELLM_API_KEY`, `VEHICLE_PIPELINE_POSTGRES_PASSWORD`, `VEHICLE_PIPELINE_BASIC_AUTH_HTPASSWD`, `VEHICLE_PIPELINE_GHCR_PULL_TOKEN`. Reuse existing `MYGARAGE_ADMIN_USERNAME`/`MYGARAGE_ADMIN_PASSWORD`/`MYGARAGE_VEHICLES` — do not create new MyGarage credentials.
- MyGarage base URL is `https://mygarage.apps.altus.janz.digital` (confirmed live, OpenAPI 3.1, title "MyGarage" "3.2.0"). Auth: `POST /api/auth/login` `{"username","password"}` → `{access_token, token_type, expires_in, csrf_token}`, then `Authorization: Bearer <access_token>` on writes.
- Two open design calls this plan makes explicitly (ticket #16 left both TBD) — flagged for approval alongside the rest of this plan, not hidden:
  1. **Own namespace + own CNPG Postgres** (`vehicle-pipeline`), not folded into `mygarage`'s namespace/DB (unlike trip-enricher). Reason: this service owns inbound webhook exposure and a Jonas-facing review UI — a different security surface and lifecycle than MyGarage's own app.
  2. **Postgres-as-queue, no Redis/valkey.** Reason: volume is a handful of documents a month (vehicle paperwork), not taxbuddy's tax-document volume — an in-process asyncio worker polling a `status` column is sufficient and avoids a second stateful pod.

---

## File Structure

```
containers/vehicle-pipeline/
  Containerfile
  pyproject.toml
  src/vehicle_pipeline/
    __init__.py
    main.py                # FastAPI app, lifespan, router wiring
    config.py               # Settings.from_env()
    db.py                    # schema DDL + IngestEventStore
    review_store.py          # ReviewQueueStore + ReviewItem
    paperless_client.py      # PaperlessClient (httpx)
    classify_vehicle.py      # pure function: tags/content -> vehicle
    extract.py                # ExtractionResult + LiteLLMExtractor
    taxonomy.py               # category -> MyGarage entity + payload mapper
    mygarage_client.py        # MyGarageClient (httpx, bearer cache)
    webhooks.py                # POST /webhooks/paperless-vehicle
    pipeline.py                 # process_document() orchestration
    reconciliation.py            # poll backstop
    review_ui.py                   # GET/POST /review*
    templates/
      review_list.html
      review_edit.html
  tests/
    conftest.py
    test_healthz.py
    test_config.py
    test_db_integration.py
    test_review_store_integration.py
    test_paperless_client.py
    test_classify_vehicle.py
    test_extract.py
    test_taxonomy.py
    test_mygarage_client.py
    test_webhooks.py
    test_pipeline.py
    test_reconciliation.py
    test_review_ui.py
    fixtures/
      paperless-webhook-document-added.json
      paperless-document-detail.json

components-apps/vehicle-pipeline/
  namespace.yaml
  external-vehicle-pipeline-credentials.yaml
  external-vehicle-pipeline-db.yaml
  postgresql-database.yaml
  external-vehicle-pipeline-basic-auth.yaml
  nginx-basic-auth-configmap.yaml
  external-vehicle-pipeline-ghcr-pull.yaml
  vehicle-pipeline-app.yaml
  kustomization.yaml

.github/workflows/build-vehicle-pipeline-image.yaml

bootstrap/overlays/local.home/values-apps.yaml   # add vehicle-pipeline entry
```

---

### Task 1: Project scaffolding + health check

**Files:**
- Create: `containers/vehicle-pipeline/pyproject.toml`
- Create: `containers/vehicle-pipeline/Containerfile`
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/__init__.py`
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/main.py`
- Test: `containers/vehicle-pipeline/tests/test_healthz.py`

**Interfaces:**
- Produces: `vehicle_pipeline.main.app` (FastAPI instance), `GET /healthz` → `{"status": "ok", "service": "vehicle-pipeline"}`.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "vehicle-pipeline"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "httpx>=0.27",
    "pydantic>=2.9",
    "jinja2>=3.1",
    "python-multipart>=0.0.12",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_healthz.py
from fastapi.testclient import TestClient

from vehicle_pipeline.main import app


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "vehicle-pipeline"}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd containers/vehicle-pipeline && pip install -e ".[dev]" && pytest tests/test_healthz.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vehicle_pipeline'`

- [ ] **Step 4: Write minimal implementation**

```python
# src/vehicle_pipeline/main.py
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_healthz.py -v`
Expected: PASS

- [ ] **Step 6: Write the Containerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
USER 1000
CMD ["uvicorn", "vehicle_pipeline.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 7: Commit**

```bash
git add containers/vehicle-pipeline/pyproject.toml containers/vehicle-pipeline/Containerfile \
  containers/vehicle-pipeline/src/vehicle_pipeline/__init__.py \
  containers/vehicle-pipeline/src/vehicle_pipeline/main.py \
  containers/vehicle-pipeline/tests/test_healthz.py
git commit -m "feat(vehicle-pipeline): scaffold service with healthz endpoint

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Config module

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/config.py`
- Test: `containers/vehicle-pipeline/tests/test_config.py`

**Interfaces:**
- Produces: `Settings` frozen dataclass with `.from_env()` classmethod; fields: `database_url`, `paperless_url`, `paperless_token`, `paperless_webhook_secret`, `litellm_base_url`, `litellm_api_key`, `mygarage_url`, `mygarage_username`, `mygarage_password`, `vehicles: dict[str, str]` (VIN → label, parsed from `MYGARAGE_VEHICLES` JSON), `poll_interval_seconds: int` (default 900).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json

import pytest

from vehicle_pipeline.config import Settings


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "PAPERLESS_URL": "https://paperless.example.test/",
        "PAPERLESS_TOKEN": "ptoken",
        "PAPERLESS_WEBHOOK_SECRET": "whsecret",
        "LITELLM_BASE_URL": "http://litellm.internal/v1",
        "LITELLM_API_KEY": "llmkey",
        "MYGARAGE_URL": "https://mygarage.example.test/",
        "MYGARAGE_USERNAME": "admin",
        "MYGARAGE_PASSWORD": "adminpw",
        "MYGARAGE_VEHICLES": json.dumps({"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"}),
    }
    base.update(overrides)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_from_env_reads_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = Settings.from_env()
    assert settings.paperless_url == "https://paperless.example.test"
    assert settings.vehicles == {"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"}
    assert settings.poll_interval_seconds == 900


def test_from_env_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("PAPERLESS_TOKEN")
    with pytest.raises(RuntimeError, match="PAPERLESS_TOKEN"):
        Settings.from_env()


def test_poll_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, POLL_INTERVAL="5m")
    assert Settings.from_env().poll_interval_seconds == 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vehicle_pipeline.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/vehicle_pipeline/config.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    paperless_url: str
    paperless_token: str
    paperless_webhook_secret: str
    litellm_base_url: str
    litellm_api_key: str
    mygarage_url: str
    mygarage_username: str
    mygarage_password: str
    vehicles: dict[str, str]
    poll_interval_seconds: int = 900

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_require("DATABASE_URL"),
            paperless_url=_require("PAPERLESS_URL").rstrip("/"),
            paperless_token=_require("PAPERLESS_TOKEN"),
            paperless_webhook_secret=_require("PAPERLESS_WEBHOOK_SECRET"),
            litellm_base_url=_require("LITELLM_BASE_URL").rstrip("/"),
            litellm_api_key=_require("LITELLM_API_KEY"),
            mygarage_url=_require("MYGARAGE_URL").rstrip("/"),
            mygarage_username=_require("MYGARAGE_USERNAME"),
            mygarage_password=_require("MYGARAGE_PASSWORD"),
            vehicles=json.loads(_require("MYGARAGE_VEHICLES")),
            poll_interval_seconds=_parse_interval(os.environ.get("POLL_INTERVAL", "15m")),
        )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _parse_interval(raw: str) -> int:
    raw = raw.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1]) * 60
    if raw.endswith("s"):
        return int(raw[:-1])
    return int(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/config.py containers/vehicle-pipeline/tests/test_config.py
git commit -m "feat(vehicle-pipeline): add env-sourced Settings

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Postgres schema + idempotency store

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/db.py`
- Test: `containers/vehicle-pipeline/tests/test_db_integration.py`
- Test: `containers/vehicle-pipeline/tests/conftest.py` (add `FakeIngestEventStore`, `test_settings` fixture)

**Interfaces:**
- Consumes: `Settings.database_url` (Task 2).
- Produces: `init_schema(pool: ConnectionPool) -> None`; `IngestEventStore` Protocol with `record_event(event_id: str, source: str, payload: dict) -> bool`; `PostgresIngestEventStore(pool)` implementing it. Later tasks (Task 9) call `record_event` to dedupe webhook/reconciliation deliveries.

- [ ] **Step 1: Write conftest fixtures**

```python
# tests/conftest.py
from __future__ import annotations

from typing import Any

import pytest

from vehicle_pipeline.config import Settings


class FakeIngestEventStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        return True


@pytest.fixture
def fake_ingest_store() -> FakeIngestEventStore:
    return FakeIngestEventStore()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="postgresql://test/test",
        paperless_url="https://paperless.example.test",
        paperless_token="ptoken",
        paperless_webhook_secret="whsecret",
        litellm_base_url="http://litellm.internal/v1",
        litellm_api_key="llmkey",
        mygarage_url="https://mygarage.example.test",
        mygarage_username="admin",
        mygarage_password="adminpw",
        vehicles={"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"},
        poll_interval_seconds=900,
    )
```

- [ ] **Step 2: Write the failing integration test**

```python
# tests/test_db_integration.py
"""Runs only when INTEGRATION_DATABASE_URL is set to a real Postgres with
init_schema() already applied — see Task 13 for port-forwarding to the
live CNPG cluster."""

import os

import pytest
from psycopg_pool import ConnectionPool

from vehicle_pipeline.db import PostgresIngestEventStore, init_schema

DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="INTEGRATION_DATABASE_URL not set")


def test_record_event_inserts_once_and_dedupes_after() -> None:
    assert DATABASE_URL is not None
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    init_schema(pool)
    store = PostgresIngestEventStore(pool)
    event_id = "paperless:test-t3-db-integration"

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM vehicle_pipeline.ingest_events WHERE event_id = %s", (event_id,))

        first = store.record_event(event_id, "paperless", {"doc_id": 999999})
        second = store.record_event(event_id, "paperless", {"doc_id": 999999})

        assert first is True
        assert second is False
    finally:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM vehicle_pipeline.ingest_events WHERE event_id = %s", (event_id,))
        pool.close()
```

- [ ] **Step 3: Run test to verify it's skipped (no DB yet) then write implementation**

Run: `pytest tests/test_db_integration.py -v`
Expected: SKIPPED (no `INTEGRATION_DATABASE_URL` set yet — this is expected until Task 13 provisions the real DB; proceed to implementation and confirm later against the real cluster).

- [ ] **Step 4: Write implementation**

```python
# src/vehicle_pipeline/db.py
from __future__ import annotations

from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS vehicle_pipeline;

CREATE TABLE IF NOT EXISTS vehicle_pipeline.ingest_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vehicle_pipeline.review_items (
    id BIGSERIAL PRIMARY KEY,
    paperless_doc_id INTEGER NOT NULL,
    paperless_doc_title TEXT NOT NULL,
    paperless_doc_url TEXT NOT NULL,
    vin TEXT,
    mygarage_entity TEXT NOT NULL,
    extracted_category TEXT NOT NULL,
    payload JSONB NOT NULL,
    confidence TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    mygarage_record_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);
"""


def init_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA_DDL)


class IngestEventStore(Protocol):
    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        """Insert the event if new. True the first time event_id is seen,
        False if it already existed (caller should treat as duplicate)."""
        ...


class PostgresIngestEventStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vehicle_pipeline.ingest_events (event_id, source, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, source, Jsonb(payload)),
            )
            return cur.rowcount > 0
```

- [ ] **Step 5: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/db.py containers/vehicle-pipeline/tests/test_db_integration.py containers/vehicle-pipeline/tests/conftest.py
git commit -m "feat(vehicle-pipeline): add schema DDL and idempotency store

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Review queue store

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/review_store.py`
- Test: `containers/vehicle-pipeline/tests/test_review_store_integration.py`

**Interfaces:**
- Consumes: `vehicle_pipeline.db.init_schema` (Task 3, same `review_items` table).
- Produces: `ReviewItem` frozen dataclass (`id`, `paperless_doc_id`, `paperless_doc_title`, `paperless_doc_url`, `vin`, `mygarage_entity`, `extracted_category`, `payload: dict`, `confidence`, `status`, `mygarage_record_id`); `ReviewQueueStore` with `create_draft(...) -> int` (returns new row id), `list_pending() -> list[ReviewItem]`, `get(id: int) -> ReviewItem | None`, `update_payload(id: int, payload: dict) -> None`, `mark_approved(id: int, mygarage_record_id: str) -> None`, `mark_rejected(id: int) -> None`. Consumed by Task 9 (pipeline creates drafts) and Task 11 (review UI reads/mutates).

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_review_store_integration.py
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
```

- [ ] **Step 2: Run test to verify it's skipped/fails**

Run: `pytest tests/test_review_store_integration.py -v`
Expected: SKIPPED until `INTEGRATION_DATABASE_URL` is set (Task 13); implement now regardless.

- [ ] **Step 3: Write implementation**

```python
# src/vehicle_pipeline/review_store.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class ReviewItem:
    id: int
    paperless_doc_id: int
    paperless_doc_title: str
    paperless_doc_url: str
    vin: str | None
    mygarage_entity: str
    extracted_category: str
    payload: dict[str, Any]
    confidence: str | None
    status: str
    mygarage_record_id: str | None


class ReviewQueueStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def create_draft(
        self,
        *,
        paperless_doc_id: int,
        paperless_doc_title: str,
        paperless_doc_url: str,
        vin: str | None,
        mygarage_entity: str,
        extracted_category: str,
        payload: dict[str, Any],
        confidence: str | None,
    ) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vehicle_pipeline.review_items
                    (paperless_doc_id, paperless_doc_title, paperless_doc_url, vin,
                     mygarage_entity, extracted_category, payload, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    paperless_doc_id,
                    paperless_doc_title,
                    paperless_doc_url,
                    vin,
                    mygarage_entity,
                    extracted_category,
                    Jsonb(payload),
                    confidence,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def list_pending(self) -> list[ReviewItem]:
        return self._select("WHERE status = 'pending' ORDER BY created_at ASC")

    def get(self, item_id: int) -> ReviewItem | None:
        items = self._select("WHERE id = %s", (item_id,))
        return items[0] if items else None

    def update_payload(self, item_id: int, payload: dict[str, Any]) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicle_pipeline.review_items SET payload = %s WHERE id = %s",
                (Jsonb(payload), item_id),
            )

    def mark_approved(self, item_id: int, mygarage_record_id: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE vehicle_pipeline.review_items
                SET status = 'approved', mygarage_record_id = %s, reviewed_at = now()
                WHERE id = %s
                """,
                (mygarage_record_id, item_id),
            )

    def mark_rejected(self, item_id: int) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE vehicle_pipeline.review_items
                SET status = 'rejected', reviewed_at = now()
                WHERE id = %s
                """,
                (item_id,),
            )

    def _select(self, where_clause: str, params: tuple[Any, ...] = ()) -> list[ReviewItem]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT id, paperless_doc_id, paperless_doc_title, paperless_doc_url, vin,
                       mygarage_entity, extracted_category, payload, confidence, status,
                       mygarage_record_id
                FROM vehicle_pipeline.review_items
                {where_clause}
                """,
                params,
            )
            return [ReviewItem(**row) for row in cur.fetchall()]
```

- [ ] **Step 4: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/review_store.py containers/vehicle-pipeline/tests/test_review_store_integration.py
git commit -m "feat(vehicle-pipeline): add review queue store

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Paperless client

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/paperless_client.py`
- Test: `containers/vehicle-pipeline/tests/test_paperless_client.py`

**Interfaces:**
- Produces: `PaperlessClient(base_url, token)` with `async get_document(doc_id: int) -> dict` (returns `{"id", "title", "content", "tags": [tag_id,...], ...}` — raw Paperless API shape), `async get_tag_names(tag_ids: list[int]) -> dict[int, str]`, `async get_tag_id(name: str) -> int | None`, `async list_documents_by_tags_modified_since(tag_ids: list[int], since_iso: str) -> list[dict]`, `document_url(doc_id: int) -> str`, `async aclose() -> None`. Consumed by Task 9 (pipeline), Task 10 (reconciliation).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paperless_client.py
import httpx
import pytest

from vehicle_pipeline.paperless_client import PaperlessClient


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_document_returns_raw_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/42/"
        assert request.headers["Authorization"] == "Token ptoken"
        return httpx.Response(200, json={"id": 42, "title": "KFZ-Steuer", "content": "text", "tags": [5, 9]})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    doc = await client.get_document(42)
    assert doc["title"] == "KFZ-Steuer"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_tag_id_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags/"
        assert request.url.params["name__iexact"] == "Fahrzeug:ID4-auto"
        return httpx.Response(200, json={"results": [{"id": 7, "name": "Fahrzeug:ID4-auto"}]})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    tag_id = await client.get_tag_id("Fahrzeug:ID4-auto")
    assert tag_id == 7
    await client.aclose()


@pytest.mark.asyncio
async def test_get_tag_id_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    assert await client.get_tag_id("Fahrzeug:Missing") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_list_documents_by_tags_modified_since() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/"
        assert request.url.params["tags__id__in"] == "7,9"
        assert request.url.params["modified__gte"] == "2026-09-01T00:00:00+00:00"
        return httpx.Response(200, json={"results": [{"id": 1}, {"id": 2}]})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    docs = await client.list_documents_by_tags_modified_since([7, 9], "2026-09-01T00:00:00+00:00")
    assert [d["id"] for d in docs] == [1, 2]
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_paperless_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# src/vehicle_pipeline/paperless_client.py
from __future__ import annotations

from typing import Any

import httpx


class PaperlessClient:
    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Token {token}"},
            transport=transport,
            timeout=30.0,
        )

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        resp = await self._client.get(f"/api/documents/{doc_id}/")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_tag_id(self, name: str) -> int | None:
        resp = await self._client.get("/api/tags/", params={"name__iexact": name})
        resp.raise_for_status()
        results = resp.json()["results"]
        return int(results[0]["id"]) if results else None

    async def get_tag_names(self, tag_ids: list[int]) -> dict[int, str]:
        names: dict[int, str] = {}
        for tag_id in tag_ids:
            resp = await self._client.get(f"/api/tags/{tag_id}/")
            resp.raise_for_status()
            names[tag_id] = resp.json()["name"]
        return names

    async def list_documents_by_tags_modified_since(
        self, tag_ids: list[int], since_iso: str
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/api/documents/",
            params={"tags__id__in": ",".join(str(t) for t in tag_ids), "modified__gte": since_iso},
        )
        resp.raise_for_status()
        return resp.json()["results"]  # type: ignore[no-any-return]

    def document_url(self, doc_id: int) -> str:
        return f"{self._base_url}/documents/{doc_id}/"

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_paperless_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/paperless_client.py containers/vehicle-pipeline/tests/test_paperless_client.py
git commit -m "feat(vehicle-pipeline): add Paperless API client

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Vehicle classifier

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/classify_vehicle.py`
- Test: `containers/vehicle-pipeline/tests/test_classify_vehicle.py`

**Interfaces:**
- Produces: `classify_vehicle(tag_names: list[str], content: str, vin_hints: dict[str, str]) -> str | None` — returns the matching VIN from `vin_hints` (a `{vin: label}` mapping, e.g. `Settings.vehicles`) or `None`. Consumed by Task 9 (pipeline).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_classify_vehicle.py
from vehicle_pipeline.classify_vehicle import classify_vehicle

VEHICLES = {"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"}


def test_classifies_by_id4_tag() -> None:
    assert classify_vehicle(["Fahrzeug:ID4-auto"], "irrelevant content", VEHICLES) == "WVGZZZE27SE017858"


def test_classifies_by_multivan_tag() -> None:
    assert classify_vehicle(["Fahrzeug:Multivan-auto"], "irrelevant", VEHICLES) == "WV2ZZZ7HZNH000000"


def test_falls_back_to_vin_in_content_when_no_tag() -> None:
    content = "Fahrzeugschein VIN: WVGZZZE27SE017858 ausgestellt am ..."
    assert classify_vehicle([], content, VEHICLES) == "WVGZZZE27SE017858"


def test_returns_none_when_no_signal() -> None:
    assert classify_vehicle(["Sonstiges"], "no vin here", VEHICLES) is None


def test_tag_takes_precedence_over_conflicting_content_vin() -> None:
    content = "mentions WV2ZZZ7HZNH000000 somewhere"
    assert classify_vehicle(["Fahrzeug:ID4-auto"], content, VEHICLES) == "WVGZZZE27SE017858"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_classify_vehicle.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# src/vehicle_pipeline/classify_vehicle.py
from __future__ import annotations

_TAG_TO_LABEL = {
    "Fahrzeug:ID4-auto": "id4",
    "Fahrzeug:Multivan-auto": "multivan",
}


def classify_vehicle(tag_names: list[str], content: str, vin_hints: dict[str, str]) -> str | None:
    label_to_vin = {label: vin for vin, label in vin_hints.items()}

    for tag_name in tag_names:
        label = _TAG_TO_LABEL.get(tag_name)
        if label is not None and label in label_to_vin:
            return label_to_vin[label]

    for vin in vin_hints:
        if vin in content:
            return vin

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_classify_vehicle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/classify_vehicle.py containers/vehicle-pipeline/tests/test_classify_vehicle.py
git commit -m "feat(vehicle-pipeline): add tag/VIN-based vehicle classifier

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: LLM extraction client

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/extract.py`
- Test: `containers/vehicle-pipeline/tests/test_extract.py`

**Interfaces:**
- Produces: `ExtractionCategory` (`Literal["kfz_steuer","haftpflicht","kasko","hu_au","finanzierung_zinsen","kraftstoff","reifenwechsel","autowaesche","oel_betriebsstoffe","adblue","ersatzteile","garantie","not_cost_relevant","other"]`), `ExtractionResult` (pydantic `BaseModel`: `category: ExtractionCategory`, `amount: float | None`, `date: str | None`, `vendor: str | None`, `odometer_km: float | None`, `notes: str | None`, `confidence: Literal["high","medium","low"]`), `LiteLLMExtractor(base_url, api_key, model="gpt-4o-mini")` with `async extract(document_text: str) -> ExtractionResult`. Raises `ExtractionError` on malformed/unparseable LLM output — caller (Task 9) catches this and still creates a review item (category `"other"`, confidence `"low"`), never drops the document.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extract.py
import json

import httpx
import pytest

from vehicle_pipeline.extract import ExtractionError, ExtractionResult, LiteLLMExtractor


def _chat_response(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
    )


@pytest.mark.asyncio
async def test_extract_parses_valid_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        assert request.headers["Authorization"] == "Bearer llmkey"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["response_format"] == {"type": "json_object"}
        return _chat_response(
            {
                "category": "kfz_steuer",
                "amount": 120.5,
                "date": "2026-03-01",
                "vendor": "Hauptzollamt",
                "odometer_km": None,
                "notes": "Jahressteuer 2026",
                "confidence": "high",
            }
        )

    extractor = LiteLLMExtractor(
        "http://litellm.internal/v1", "llmkey", transport=httpx.MockTransport(handler)
    )
    result = await extractor.extract("KFZ-Steuerbescheid ... 120,50 EUR ...")
    assert isinstance(result, ExtractionResult)
    assert result.category == "kfz_steuer"
    assert result.amount == 120.5
    await extractor.aclose()


@pytest.mark.asyncio
async def test_extract_raises_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    extractor = LiteLLMExtractor(
        "http://litellm.internal/v1", "llmkey", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ExtractionError):
        await extractor.extract("some text")
    await extractor.aclose()


@pytest.mark.asyncio
async def test_extract_raises_on_invalid_category() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response({"category": "not_a_real_category", "confidence": "high"})

    extractor = LiteLLMExtractor(
        "http://litellm.internal/v1", "llmkey", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ExtractionError):
        await extractor.extract("some text")
    await extractor.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_extract.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# src/vehicle_pipeline/extract.py
from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

ExtractionCategory = Literal[
    "kfz_steuer",
    "haftpflicht",
    "kasko",
    "hu_au",
    "finanzierung_zinsen",
    "kraftstoff",
    "reifenwechsel",
    "autowaesche",
    "oel_betriebsstoffe",
    "adblue",
    "ersatzteile",
    "garantie",
    "not_cost_relevant",
    "other",
]

_SYSTEM_PROMPT = """Du extrahierst Kostendaten aus deutschsprachigen Fahrzeugdokumenten \
(Steuerbescheide, Versicherungen, Werkstattrechnungen, Tankquittungen, Garantien, etc.).

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt mit genau diesen Feldern:
- category: einer von kfz_steuer, haftpflicht, kasko, hu_au, finanzierung_zinsen, \
kraftstoff, reifenwechsel, autowaesche, oel_betriebsstoffe, adblue, ersatzteile, \
garantie, not_cost_relevant, other
- amount: Betrag in EUR als Zahl, oder null
- date: Datum im Format YYYY-MM-DD, oder null
- vendor: Name des Ausstellers/Anbieters, oder null
- odometer_km: Kilometerstand falls im Dokument genannt, sonst null
- notes: kurze Freitext-Notiz (max. 200 Zeichen), oder null
- confidence: high, medium oder low

Nutze "not_cost_relevant", wenn das Dokument keine Kostenposition enthält \
(z.B. reine Korrespondenz). Nutze "other", wenn ein Kostendokument keiner der \
übrigen Kategorien zugeordnet werden kann."""


class ExtractionResult(BaseModel):
    category: ExtractionCategory
    amount: float | None = None
    date: str | None = None
    vendor: str | None = None
    odometer_km: float | None = None
    notes: str | None = None
    confidence: Literal["high", "medium", "low"]


class ExtractionError(Exception):
    pass


class LiteLLMExtractor:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gpt-4o-mini",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=60.0,
        )

    async def extract(self, document_text: str) -> ExtractionResult:
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Dokumenttext:\n{document_text[:8000]}"},
                ],
            },
        )
        resp.raise_for_status()
        raw_content = resp.json()["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"LLM did not return valid JSON: {raw_content!r}") from exc
        try:
            return ExtractionResult.model_validate(parsed)
        except ValidationError as exc:
            raise ExtractionError(f"LLM JSON failed schema validation: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_extract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/extract.py containers/vehicle-pipeline/tests/test_extract.py
git commit -m "feat(vehicle-pipeline): add LiteLLM-backed cost extraction

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Taxonomy mapper + MyGarage client

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/taxonomy.py`
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/mygarage_client.py`
- Test: `containers/vehicle-pipeline/tests/test_taxonomy.py`
- Test: `containers/vehicle-pipeline/tests/test_mygarage_client.py`

**Interfaces:**
- Produces (`taxonomy.py`): `MyGarageDraft` (frozen dataclass: `entity: str`, `payload: dict`); `map_to_mygarage(extraction: ExtractionResult, vin: str) -> MyGarageDraft`. Categories `not_cost_relevant`/unmapped fall back to `entity="documents"`.
- Produces (`mygarage_client.py`): `MyGarageClient(base_url, username, password)` with `async create_record(vin: str, entity: str, payload: dict) -> dict` (dispatches to the correct `/api/vehicles/{vin}/{entity}` POST, returns created record JSON) and `async aclose()`. Bearer token is fetched lazily on first call and cached for the process lifetime (short-lived tokens are fine — a 401 triggers one re-login retry). Consumed by Task 11 (review UI approve action).

- [ ] **Step 1: Write the failing taxonomy test**

```python
# tests/test_taxonomy.py
from vehicle_pipeline.extract import ExtractionResult
from vehicle_pipeline.taxonomy import map_to_mygarage

VIN = "WVGZZZE27SE017858"


def _result(**overrides) -> ExtractionResult:
    base = dict(category="kfz_steuer", amount=120.5, date="2026-03-01", vendor="Hauptzollamt",
                odometer_km=None, notes="Jahressteuer", confidence="high")
    base.update(overrides)
    return ExtractionResult.model_validate(base)


def test_kfz_steuer_maps_to_tax_records() -> None:
    draft = map_to_mygarage(_result(), VIN)
    assert draft.entity == "tax-records"
    assert draft.payload == {
        "vin": VIN, "date": "2026-03-01", "amount": 120.5,
        "tax_type": "KFZ-Steuer", "notes": "Jahressteuer",
    }


def test_haftpflicht_maps_to_insurance() -> None:
    draft = map_to_mygarage(_result(category="haftpflicht", vendor="HUK24"), VIN)
    assert draft.entity == "insurance"
    assert draft.payload["policy_type"] == "Haftpflicht"
    assert draft.payload["provider"] == "HUK24"
    assert draft.payload["premium_amount"] == 120.5


def test_hu_au_maps_to_service_visits_inspection() -> None:
    draft = map_to_mygarage(_result(category="hu_au", odometer_km=54000), VIN)
    assert draft.entity == "service-visits"
    assert draft.payload["service_category"] == "Inspection"
    assert draft.payload["odometer_km"] == 54000
    assert draft.payload["line_items"] == []


def test_kraftstoff_maps_to_fuel() -> None:
    draft = map_to_mygarage(_result(category="kraftstoff", amount=65.0), VIN)
    assert draft.entity == "fuel"
    assert draft.payload["cost"] == 65.0


def test_adblue_maps_to_def() -> None:
    draft = map_to_mygarage(_result(category="adblue", amount=15.0), VIN)
    assert draft.entity == "def"


def test_garantie_maps_to_warranties() -> None:
    draft = map_to_mygarage(_result(category="garantie", vendor="VW Garantie"), VIN)
    assert draft.entity == "warranties"
    assert draft.payload["warranty_type"] == "VW Garantie"


def test_unmapped_category_falls_back_to_documents() -> None:
    draft = map_to_mygarage(_result(category="finanzierung_zinsen"), VIN)
    assert draft.entity == "documents"


def test_not_cost_relevant_falls_back_to_documents() -> None:
    draft = map_to_mygarage(_result(category="not_cost_relevant", amount=None), VIN)
    assert draft.entity == "documents"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_taxonomy.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write taxonomy implementation**

```python
# src/vehicle_pipeline/taxonomy.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vehicle_pipeline.extract import ExtractionResult


@dataclass(frozen=True)
class MyGarageDraft:
    entity: str
    payload: dict[str, Any]


def map_to_mygarage(extraction: ExtractionResult, vin: str) -> MyGarageDraft:
    builder = _BUILDERS.get(extraction.category, _build_documents)
    return builder(extraction, vin)


def _build_tax_record(e: ExtractionResult, vin: str) -> MyGarageDraft:
    return MyGarageDraft("tax-records", {
        "vin": vin, "date": e.date, "amount": e.amount,
        "tax_type": "KFZ-Steuer", "notes": e.notes,
    })


def _build_insurance(policy_type: str):
    def _build(e: ExtractionResult, vin: str) -> MyGarageDraft:
        return MyGarageDraft("insurance", {
            "provider": e.vendor or "",
            "policy_number": "",
            "policy_type": policy_type,
            "start_date": e.date or "",
            "end_date": "",
            "premium_amount": e.amount,
            "notes": e.notes,
        })
    return _build


def _build_service_visit(category: str):
    def _build(e: ExtractionResult, vin: str) -> MyGarageDraft:
        return MyGarageDraft("service-visits", {
            "date": e.date,
            "odometer_km": e.odometer_km,
            "service_category": category,
            "notes": e.notes,
            "total_cost": e.amount,
            "line_items": [],
        })
    return _build


def _build_fuel(e: ExtractionResult, vin: str) -> MyGarageDraft:
    return MyGarageDraft("fuel", {
        "vin": vin, "date": e.date, "cost": e.amount, "notes": e.notes,
    })


def _build_def(e: ExtractionResult, vin: str) -> MyGarageDraft:
    return MyGarageDraft("def", {
        "vin": vin, "date": e.date, "cost": e.amount, "notes": e.notes,
    })


def _build_warranty(e: ExtractionResult, vin: str) -> MyGarageDraft:
    return MyGarageDraft("warranties", {
        "warranty_type": e.vendor or "Garantie",
        "start_date": e.date or "",
        "notes": e.notes,
    })


def _build_documents(e: ExtractionResult, vin: str) -> MyGarageDraft:
    return MyGarageDraft("documents", {
        "title": e.vendor or e.category,
        "document_type": e.category,
        "description": e.notes,
    })


_BUILDERS = {
    "kfz_steuer": _build_tax_record,
    "haftpflicht": _build_insurance("Haftpflicht"),
    "kasko": _build_insurance("Kasko"),
    "hu_au": _build_service_visit("Inspection"),
    "reifenwechsel": _build_service_visit("Maintenance"),
    "autowaesche": _build_service_visit("Detailing"),
    "oel_betriebsstoffe": _build_service_visit("Maintenance"),
    "kraftstoff": _build_fuel,
    "adblue": _build_def,
    "garantie": _build_warranty,
}
```

- [ ] **Step 4: Run taxonomy test to verify it passes**

Run: `pytest tests/test_taxonomy.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing MyGarage client test**

```python
# tests/test_mygarage_client.py
import httpx
import pytest

from vehicle_pipeline.mygarage_client import MyGarageClient


@pytest.mark.asyncio
async def test_create_record_logs_in_then_posts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/auth/login":
            body = httpx.Request("POST", request.url, content=request.content)
            import json as _json
            assert _json.loads(request.content) == {"username": "admin", "password": "adminpw"}
            return httpx.Response(200, json={"access_token": "tok123", "token_type": "bearer",
                                              "expires_in": 3600, "csrf_token": "csrf"})
        assert request.url.path == "/api/vehicles/WVGZZZE27SE017858/tax-records"
        assert request.headers["Authorization"] == "Bearer tok123"
        return httpx.Response(201, json={"id": 99, "amount": 120.5})

    client = MyGarageClient(
        "https://mygarage.example.test", "admin", "adminpw", transport=httpx.MockTransport(handler)
    )
    result = await client.create_record(
        "WVGZZZE27SE017858", "tax-records",
        {"vin": "WVGZZZE27SE017858", "date": "2026-03-01", "amount": 120.5},
    )
    assert result == {"id": 99, "amount": 120.5}
    assert calls == ["/api/auth/login", "/api/vehicles/WVGZZZE27SE017858/tax-records"]
    await client.aclose()


@pytest.mark.asyncio
async def test_create_record_reuses_cached_token() -> None:
    login_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        if request.url.path == "/api/auth/login":
            login_calls += 1
            return httpx.Response(200, json={"access_token": "tok123", "token_type": "bearer",
                                              "expires_in": 3600, "csrf_token": "csrf"})
        return httpx.Response(201, json={"id": 1})

    client = MyGarageClient(
        "https://mygarage.example.test", "admin", "adminpw", transport=httpx.MockTransport(handler)
    )
    await client.create_record("VIN1", "def", {"vin": "VIN1", "date": "2026-01-01"})
    await client.create_record("VIN1", "def", {"vin": "VIN1", "date": "2026-01-02"})
    assert login_calls == 1
    await client.aclose()
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_mygarage_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 7: Write MyGarage client implementation**

```python
# src/vehicle_pipeline/mygarage_client.py
from __future__ import annotations

from typing import Any

import httpx

_ENTITY_PATHS = {
    "fuel": "fuel",
    "service-visits": "service-visits",
    "insurance": "insurance",
    "warranties": "warranties",
    "tax-records": "tax-records",
    "def": "def",
    "documents": "documents",
}


class MyGarageClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), transport=transport, timeout=30.0
        )

    async def create_record(self, vin: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = _ENTITY_PATHS[entity]
        token = await self._get_token()
        resp = await self._client.post(
            f"/api/vehicles/{vin}/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _get_token(self) -> str:
        if self._token is None:
            resp = await self._client.post(
                "/api/auth/login",
                json={"username": self._username, "password": self._password},
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
        return self._token

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_mygarage_client.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/taxonomy.py containers/vehicle-pipeline/src/vehicle_pipeline/mygarage_client.py \
  containers/vehicle-pipeline/tests/test_taxonomy.py containers/vehicle-pipeline/tests/test_mygarage_client.py
git commit -m "feat(vehicle-pipeline): add taxonomy mapper and MyGarage write client

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: Webhook route + pipeline orchestration

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/pipeline.py`
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/webhooks.py`
- Test: `containers/vehicle-pipeline/tests/fixtures/paperless-webhook-document-added.json`
- Test: `containers/vehicle-pipeline/tests/test_pipeline.py`
- Test: `containers/vehicle-pipeline/tests/test_webhooks.py`
- Modify: `containers/vehicle-pipeline/src/vehicle_pipeline/main.py` (wire router + app.state)

**Interfaces:**
- Consumes: `IngestEventStore.record_event` (Task 3), `ReviewQueueStore.create_draft` (Task 4), `PaperlessClient.get_document` (Task 5), `classify_vehicle` (Task 6), `LiteLLMExtractor.extract`/`ExtractionError` (Task 7), `map_to_mygarage` (Task 8).
- Produces: `pipeline.process_document(doc_id, paperless, llm, review_store, vehicles) -> None`; `webhooks.router` (FastAPI `APIRouter`) with `POST /webhooks/paperless-vehicle`. Consumed by Task 10 (reconciliation reuses `process_document`) and Task 12 (main.py wiring).

- [ ] **Step 1: Write the fixture**

```json
{
  "doc_id": 42,
  "doc_title": "KFZ-Steuerbescheid 2026",
  "doc_url": "https://paperless.example.test/documents/42/",
  "correspondent": "Hauptzollamt",
  "document_type": "Steuerbescheid",
  "added": "2026-03-01T10:00:00+01:00",
  "owner_username": "jonas",
  "filename": "kfz-steuer-2026.pdf"
}
```

- [ ] **Step 2: Write the failing pipeline test**

```python
# tests/test_pipeline.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Write pipeline implementation**

```python
# src/vehicle_pipeline/pipeline.py
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
```

- [ ] **Step 5: Run pipeline test to verify it passes**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS

- [ ] **Step 6: Write the failing webhook test**

```python
# tests/test_webhooks.py
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from vehicle_pipeline.main import app
from vehicle_pipeline.webhooks import get_ingest_store, get_settings, get_review_store, get_paperless, get_llm

FIXTURE = Path(__file__).parent / "fixtures" / "paperless-webhook-document-added.json"


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _client(fake_ingest_store: Any, test_settings: Any) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_ingest_store] = lambda: fake_ingest_store
    app.dependency_overrides[get_review_store] = lambda: object()
    app.dependency_overrides[get_paperless] = lambda: object()
    app.dependency_overrides[get_llm] = lambda: object()
    return TestClient(app)


def test_valid_signature_is_queued(fake_ingest_store: Any, test_settings: Any) -> None:
    client = _client(fake_ingest_store, test_settings)
    resp = client.post(
        "/webhooks/paperless-vehicle",
        content=json.dumps(_payload()),
        headers={"X-Vehicle-Pipeline-Signature": "whsecret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
    app.dependency_overrides.clear()


def test_missing_signature_is_rejected(fake_ingest_store: Any, test_settings: Any) -> None:
    client = _client(fake_ingest_store, test_settings)
    resp = client.post("/webhooks/paperless-vehicle", content=json.dumps(_payload()))
    assert resp.status_code == 401
    app.dependency_overrides.clear()


def test_wrong_signature_is_rejected(fake_ingest_store: Any, test_settings: Any) -> None:
    client = _client(fake_ingest_store, test_settings)
    resp = client.post(
        "/webhooks/paperless-vehicle",
        content=json.dumps(_payload()),
        headers={"X-Vehicle-Pipeline-Signature": "wrong"},
    )
    assert resp.status_code == 401
    app.dependency_overrides.clear()


def test_malformed_json_is_rejected(fake_ingest_store: Any, test_settings: Any) -> None:
    client = _client(fake_ingest_store, test_settings)
    resp = client.post(
        "/webhooks/paperless-vehicle",
        content=b"not json",
        headers={"X-Vehicle-Pipeline-Signature": "whsecret"},
    )
    assert resp.status_code == 400
    app.dependency_overrides.clear()


def test_duplicate_delivery_is_reported_once(fake_ingest_store: Any, test_settings: Any) -> None:
    client = _client(fake_ingest_store, test_settings)
    body = json.dumps(_payload())
    headers = {"X-Vehicle-Pipeline-Signature": "whsecret"}
    first = client.post("/webhooks/paperless-vehicle", content=body, headers=headers)
    second = client.post("/webhooks/paperless-vehicle", content=body, headers=headers)
    assert first.json() == {"status": "queued"}
    assert second.json() == {"status": "duplicate"}
    app.dependency_overrides.clear()
```

- [ ] **Step 7: Run test to verify it fails**

Run: `pytest tests/test_webhooks.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 8: Write webhook route implementation**

```python
# src/vehicle_pipeline/webhooks.py
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
        process_document, webhook.doc_id, paperless, llm, review_store, settings.vehicles
    )
    logger.info("queued document %s for processing", webhook.doc_id)
    return JSONResponse({"status": "queued"})
```

- [ ] **Step 9: Wire main.py to include the router with placeholder app.state (real wiring lands in Task 12)**

```python
# src/vehicle_pipeline/main.py — replace prior content
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
```

- [ ] **Step 10: Run webhook test to verify it passes**

Run: `pytest tests/test_webhooks.py tests/test_healthz.py -v`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/pipeline.py containers/vehicle-pipeline/src/vehicle_pipeline/webhooks.py \
  containers/vehicle-pipeline/src/vehicle_pipeline/main.py containers/vehicle-pipeline/tests/test_pipeline.py \
  containers/vehicle-pipeline/tests/test_webhooks.py containers/vehicle-pipeline/tests/fixtures/paperless-webhook-document-added.json
git commit -m "feat(vehicle-pipeline): add webhook route and processing pipeline

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: Poll reconciliation backstop

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/reconciliation.py`
- Test: `containers/vehicle-pipeline/tests/test_reconciliation.py`

**Interfaces:**
- Consumes: `PaperlessClient.get_tag_id`/`list_documents_by_tags_modified_since` (Task 5), `IngestEventStore.record_event` (Task 3), `pipeline.process_document` (Task 9).
- Produces: `run_reconciliation_pass(paperless, ingest_store, llm, review_store, vehicles, watermark_store, now=None) -> int` (returns count enqueued); `reconciliation_loop(interval_seconds, ...) -> None` (infinite loop, catches and logs exceptions per iteration so one bad pass doesn't kill the loop). `WatermarkStore` Protocol (`get() -> str | None`, `set(iso: str) -> None`) — a thin wrapper the plan backs with a one-row Postgres table (added to Task 3's schema retroactively here since it's small and only reconciliation needs it).

- [ ] **Step 1: Add the watermark table to schema DDL**

```python
# src/vehicle_pipeline/db.py — add to _SCHEMA_DDL, after review_items table:
```

```sql
CREATE TABLE IF NOT EXISTS vehicle_pipeline.reconciliation_watermark (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_run_at TIMESTAMPTZ
);
INSERT INTO vehicle_pipeline.reconciliation_watermark (id, last_run_at)
VALUES (1, NULL) ON CONFLICT (id) DO NOTHING;
```

Also add to `db.py`:

```python
class PostgresWatermarkStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def get(self) -> str | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT last_run_at FROM vehicle_pipeline.reconciliation_watermark WHERE id = 1")
            row = cur.fetchone()
            return row[0].isoformat() if row and row[0] else None

    def set(self, iso: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE vehicle_pipeline.reconciliation_watermark SET last_run_at = %s WHERE id = 1",
                (iso,),
            )
```

- [ ] **Step 2: Write the failing reconciliation test**

```python
# tests/test_reconciliation.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_reconciliation.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Write implementation**

```python
# src/vehicle_pipeline/reconciliation.py
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_reconciliation.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/reconciliation.py containers/vehicle-pipeline/src/vehicle_pipeline/db.py \
  containers/vehicle-pipeline/tests/test_reconciliation.py
git commit -m "feat(vehicle-pipeline): add poll reconciliation backstop

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: Review UI

**Files:**
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/review_ui.py`
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/templates/review_list.html`
- Create: `containers/vehicle-pipeline/src/vehicle_pipeline/templates/review_edit.html`
- Test: `containers/vehicle-pipeline/tests/test_review_ui.py`

**Interfaces:**
- Consumes: `ReviewQueueStore` (Task 4), `MyGarageClient.create_record` (Task 8).
- Produces: `review_ui.router` with `GET /review`, `GET /review/{item_id}/edit`, `POST /review/{item_id}/approve`, `POST /review/{item_id}/reject`. Consumed by Task 12 (main.py wiring).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_ui.py
from typing import Any

from fastapi.testclient import TestClient

from vehicle_pipeline.main import app
from vehicle_pipeline.review_store import ReviewItem
from vehicle_pipeline.review_ui import get_mygarage, get_review_store


class FakeReviewStore:
    def __init__(self, items: list[ReviewItem]) -> None:
        self._items = {item.id: item for item in items}
        self.approved: list[tuple[int, str]] = []
        self.rejected: list[int] = []
        self.updated_payloads: list[tuple[int, dict]] = []

    def list_pending(self) -> list[ReviewItem]:
        return [i for i in self._items.values() if i.status == "pending"]

    def get(self, item_id: int) -> ReviewItem | None:
        return self._items.get(item_id)

    def update_payload(self, item_id: int, payload: dict[str, Any]) -> None:
        self.updated_payloads.append((item_id, payload))
        item = self._items[item_id]
        self._items[item_id] = ReviewItem(**{**item.__dict__, "payload": payload})

    def mark_approved(self, item_id: int, mygarage_record_id: str) -> None:
        self.approved.append((item_id, mygarage_record_id))
        item = self._items[item_id]
        self._items[item_id] = ReviewItem(**{**item.__dict__, "status": "approved"})

    def mark_rejected(self, item_id: int) -> None:
        self.rejected.append(item_id)
        item = self._items[item_id]
        self._items[item_id] = ReviewItem(**{**item.__dict__, "status": "rejected"})


class FakeMyGarage:
    async def create_record(self, vin: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": "created-1"}


def _item(item_id: int = 1) -> ReviewItem:
    return ReviewItem(
        id=item_id, paperless_doc_id=42, paperless_doc_title="KFZ-Steuer 2026",
        paperless_doc_url="https://paperless.example.test/documents/42/",
        vin="WVGZZZE27SE017858", mygarage_entity="tax-records", extracted_category="kfz_steuer",
        payload={"vin": "WVGZZZE27SE017858", "date": "2026-03-01", "amount": 120.5,
                 "tax_type": "KFZ-Steuer", "notes": None},
        confidence="high", status="pending", mygarage_record_id=None,
    )


def test_review_list_shows_pending_items() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.get("/review")
    assert resp.status_code == 200
    assert "KFZ-Steuer 2026" in resp.text
    app.dependency_overrides.clear()


def test_approve_commits_to_mygarage_and_marks_approved() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/1/approve", data={
        "vin": "WVGZZZE27SE017858", "date": "2026-03-01", "amount": "125.0",
        "tax_type": "KFZ-Steuer", "notes": "corrected amount",
    })

    assert resp.status_code in (200, 303)
    assert store.approved == [(1, "created-1")]
    assert store.updated_payloads[-1][1]["amount"] == 125.0
    app.dependency_overrides.clear()


def test_reject_marks_rejected_without_calling_mygarage() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/1/reject")

    assert resp.status_code in (200, 303)
    assert store.rejected == [1]
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_review_ui.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the templates**

```html
<!-- src/vehicle_pipeline/templates/review_list.html -->
<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>Vehicle Pipeline — Review</title></head>
<body>
<h1>Offene Kostenbelege</h1>
<table border="1" cellpadding="6">
  <tr><th>Dokument</th><th>Fahrzeug</th><th>Kategorie</th><th>Ziel</th><th>Betrag</th><th>Confidence</th><th></th></tr>
  {% for item in items %}
  <tr>
    <td><a href="{{ item.paperless_doc_url }}" target="_blank">{{ item.paperless_doc_title }}</a></td>
    <td>{{ item.vin or "unbekannt" }}</td>
    <td>{{ item.extracted_category }}</td>
    <td>{{ item.mygarage_entity }}</td>
    <td>{{ item.payload.get("amount") or item.payload.get("total_cost") or item.payload.get("cost") or item.payload.get("premium_amount") or "-" }}</td>
    <td>{{ item.confidence }}</td>
    <td><a href="/review/{{ item.id }}/edit">Prüfen</a></td>
  </tr>
  {% endfor %}
</table>
</body>
</html>
```

```html
<!-- src/vehicle_pipeline/templates/review_edit.html -->
<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>Beleg prüfen</title></head>
<body>
<h1>{{ item.paperless_doc_title }}</h1>
<p><a href="{{ item.paperless_doc_url }}" target="_blank">Original in Paperless öffnen</a></p>
<form method="post" action="/review/{{ item.id }}/approve">
  {% for key, value in item.payload.items() %}
  <label>{{ key }}: <input type="text" name="{{ key }}" value="{{ value if value is not none else '' }}"></label><br>
  {% endfor %}
  <button type="submit">Übernehmen (an MyGarage senden)</button>
</form>
<form method="post" action="/review/{{ item.id }}/reject">
  <button type="submit">Ablehnen</button>
</form>
</body>
</html>
```

- [ ] **Step 4: Write implementation**

```python
# src/vehicle_pipeline/review_ui.py
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_review_store(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.review_store


def get_mygarage(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.mygarage


@router.get("/review", response_class=HTMLResponse)
def list_review_items(request: Request, review_store=Depends(get_review_store)) -> HTMLResponse:  # noqa: B008
    items = review_store.list_pending()
    return _templates.TemplateResponse(request, "review_list.html", {"items": items})


@router.get("/review/{item_id}/edit", response_class=HTMLResponse)
def edit_review_item(
    request: Request, item_id: int, review_store=Depends(get_review_store)  # noqa: B008
) -> HTMLResponse:
    item = review_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    return _templates.TemplateResponse(request, "review_edit.html", {"item": item})


@router.post("/review/{item_id}/approve")
async def approve_review_item(
    request: Request,
    item_id: int,
    review_store=Depends(get_review_store),  # noqa: B008
    mygarage=Depends(get_mygarage),  # noqa: B008
) -> RedirectResponse:
    item = review_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")

    form = await request.form()
    payload: dict[str, Any] = dict(item.payload)
    for key in payload:
        if key in form:
            payload[key] = _coerce(form[key], type(item.payload[key]))
    review_store.update_payload(item_id, payload)

    result = await mygarage.create_record(item.vin or "", item.mygarage_entity, payload)
    review_store.mark_approved(item_id, str(result.get("id", "")))
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/{item_id}/reject")
def reject_review_item(item_id: int, review_store=Depends(get_review_store)) -> RedirectResponse:  # noqa: B008
    if review_store.get(item_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    review_store.mark_rejected(item_id)
    return RedirectResponse(url="/review", status_code=303)


def _coerce(raw: Any, original_type: type) -> Any:
    if raw == "":
        return None
    if original_type is float:
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_review_ui.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/review_ui.py containers/vehicle-pipeline/src/vehicle_pipeline/templates/ \
  containers/vehicle-pipeline/tests/test_review_ui.py
git commit -m "feat(vehicle-pipeline): add review queue UI

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: Wire main.py end-to-end + finalize Containerfile

**Files:**
- Modify: `containers/vehicle-pipeline/src/vehicle_pipeline/main.py`
- Modify: `containers/vehicle-pipeline/Containerfile`

**Interfaces:**
- Consumes: everything from Tasks 2–11.
- Produces: the fully wired `app` — this is the artifact Task 13's Deployment runs.

- [ ] **Step 1: Rewrite main.py with full lifespan wiring**

```python
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
```

- [ ] **Step 2: Run the full test suite to verify nothing broke**

Run: `cd containers/vehicle-pipeline && pytest -v`
Expected: PASS (all tests except the two `INTEGRATION_DATABASE_URL`-gated ones, which SKIP)

- [ ] **Step 3: Update Containerfile to copy templates and install non-dev deps only**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
USER 1000
EXPOSE 8000
CMD ["uvicorn", "vehicle_pipeline.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

(No change needed if Task 1's Containerfile already `COPY src/` wholesale — confirm `src/vehicle_pipeline/templates/*.html` is included, since Jinja2Templates reads from disk at request time, not bundled by pip.)

- [ ] **Step 4: Local smoke test**

```bash
cd containers/vehicle-pipeline
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
export PAPERLESS_URL="https://paperless.example.test" PAPERLESS_TOKEN=x PAPERLESS_WEBHOOK_SECRET=x
export LITELLM_BASE_URL="http://localhost:4000/v1" LITELLM_API_KEY=x
export MYGARAGE_URL="https://mygarage.example.test" MYGARAGE_USERNAME=x MYGARAGE_PASSWORD=x
export MYGARAGE_VEHICLES='{"WVGZZZE27SE017858":"id4"}'
# requires a local Postgres — skip if unavailable, real verification happens against
# the live cluster in Task 13 via `oc port-forward`
uvicorn vehicle_pipeline.main:app --reload &
curl -s localhost:8000/healthz
kill %1
```

Expected: `{"status":"ok","service":"vehicle-pipeline"}` (if Postgres wasn't reachable, this step is deferred to Task 13's live verification — note that in the task's completion report rather than blocking on it).

- [ ] **Step 5: Commit**

```bash
git add containers/vehicle-pipeline/src/vehicle_pipeline/main.py containers/vehicle-pipeline/Containerfile
git commit -m "feat(vehicle-pipeline): wire full app lifespan

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 13: Deployment manifests

**Files:**
- Create: `components-apps/vehicle-pipeline/namespace.yaml`
- Create: `components-apps/vehicle-pipeline/postgresql-database.yaml`
- Create: `components-apps/vehicle-pipeline/external-vehicle-pipeline-db.yaml`
- Create: `components-apps/vehicle-pipeline/external-vehicle-pipeline-credentials.yaml`
- Create: `components-apps/vehicle-pipeline/external-vehicle-pipeline-basic-auth.yaml`
- Create: `components-apps/vehicle-pipeline/nginx-basic-auth-configmap.yaml`
- Create: `components-apps/vehicle-pipeline/external-vehicle-pipeline-ghcr-pull.yaml`
- Create: `components-apps/vehicle-pipeline/vehicle-pipeline-app.yaml`
- Create: `components-apps/vehicle-pipeline/kustomization.yaml`
- Modify: `bootstrap/overlays/local.home/values-apps.yaml`

**Interfaces:**
- Consumes: Doppler keys listed in Global Constraints (created in Task 15 — this task can be authored and committed before Task 15 runs, but `kustomize build` / ArgoCD sync will only go healthy once those keys exist).
- Produces: a running `vehicle-pipeline` namespace on Altus with a `/webhooks/paperless-vehicle` path (no auth beyond the app's own shared-secret check) and a `/review` path (nginx basic-auth sidecar, mirrors `pricing-tool`).

- [ ] **Step 1: namespace.yaml**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: vehicle-pipeline
```

- [ ] **Step 2: postgresql-database.yaml (mirrors taxbuddy-db)**

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: vehicle-pipeline-db
  namespace: vehicle-pipeline
  annotations:
    argocd.argoproj.io/sync-wave: "100"
spec:
  instances: 1
  enablePDB: false
  bootstrap:
    initdb:
      database: vehicle_pipeline
      owner: vehicle_pipeline
      secret:
        name: vehicle-pipeline-db-app-secret
  storage:
    size: 5Gi
    storageClass: lvms-vg1
```

- [ ] **Step 3: external-vehicle-pipeline-db.yaml**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: vehicle-pipeline-db-app-secret
  namespace: vehicle-pipeline
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: vehicle-pipeline-db-app-secret
    template:
      engineVersion: v2
      type: kubernetes.io/basic-auth
      data:
        username: "vehicle_pipeline"
        password: "{{ .password }}"
  data:
    - secretKey: password
      remoteRef:
        key: VEHICLE_PIPELINE_POSTGRES_PASSWORD
```

- [ ] **Step 4: external-vehicle-pipeline-credentials.yaml**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: vehicle-pipeline-credentials
  namespace: vehicle-pipeline
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: vehicle-pipeline-credentials
  data:
    - secretKey: PAPERLESS_URL
      remoteRef:
        key: VEHICLE_PIPELINE_PAPERLESS_URL
    - secretKey: PAPERLESS_TOKEN
      remoteRef:
        key: VEHICLE_PIPELINE_PAPERLESS_TOKEN
    - secretKey: PAPERLESS_WEBHOOK_SECRET
      remoteRef:
        key: VEHICLE_PIPELINE_PAPERLESS_WEBHOOK_SECRET
    - secretKey: LITELLM_BASE_URL
      remoteRef:
        key: VEHICLE_PIPELINE_LITELLM_BASE_URL
    - secretKey: LITELLM_API_KEY
      remoteRef:
        key: VEHICLE_PIPELINE_LITELLM_API_KEY
    - secretKey: MYGARAGE_USERNAME
      remoteRef:
        key: MYGARAGE_ADMIN_USERNAME
    - secretKey: MYGARAGE_PASSWORD
      remoteRef:
        key: MYGARAGE_ADMIN_PASSWORD
    - secretKey: MYGARAGE_VEHICLES
      remoteRef:
        key: MYGARAGE_VEHICLES
```

- [ ] **Step 5: external-vehicle-pipeline-basic-auth.yaml + nginx-basic-auth-configmap.yaml (mirrors pricing-tool)**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: vehicle-pipeline-basic-auth
  namespace: vehicle-pipeline
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: vehicle-pipeline-basic-auth
  data:
    - secretKey: htpasswd
      remoteRef:
        key: VEHICLE_PIPELINE_BASIC_AUTH_HTPASSWD
```

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: vehicle-pipeline-nginx-basic-auth
  namespace: vehicle-pipeline
data:
  nginx.conf: |
    events {}
    http {
      server {
        listen 8080;
        location / {
          auth_basic "vehicle-pipeline review";
          auth_basic_user_file /etc/nginx/htpasswd/htpasswd;
          proxy_pass http://127.0.0.1:8000;
        }
      }
    }
```

*(Reference `components-apps/pricing-tool/nginx-basic-auth-configmap.yaml` and its sidecar wiring in `pricing-tool-app.yaml` for the exact volume-mount shape before finalizing this — copy that pattern verbatim, substituting names.)*

- [ ] **Step 6: external-vehicle-pipeline-ghcr-pull.yaml (mirrors taxbuddy's)**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: vehicle-pipeline-ghcr-pull
  namespace: vehicle-pipeline
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: vehicle-pipeline-ghcr-pull-secret
    template:
      type: kubernetes.io/dockerconfigjson
      data:
        .dockerconfigjson: |
          {"auths":{"ghcr.io":{"username":"pixeljonas","password":"{{ .token }}"}}}
  data:
    - secretKey: token
      remoteRef:
        key: VEHICLE_PIPELINE_GHCR_PULL_TOKEN
```

*(Confirm the exact template shape against `components-apps/taxbuddy/external-taxbuddy-ghcr-pull.yaml` before finalizing — copy it verbatim, substituting names/keys.)*

- [ ] **Step 7: vehicle-pipeline-app.yaml**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: vehicle-pipeline-app
  namespace: openshift-gitops
  annotations:
    argocd.argoproj.io/sync-wave: "111"
spec:
  destination:
    namespace: vehicle-pipeline
    server: "https://kubernetes.default.svc"
  source:
    chart: app-template
    repoURL: https://bjw-s-labs.github.io/helm-charts
    targetRevision: 5.1.0
    helm:
      values: |-
        defaultPodOptions:
          imagePullSecrets:
            - name: vehicle-pipeline-ghcr-pull-secret

        controllers:
          app:
            replicas: 1
            containers:
              app:
                image:
                  repository: ghcr.io/pixeljonas/vehicle-pipeline
                  tag: latest
                  pullPolicy: Always
                env:
                  TZ: Europe/Berlin
                  DATABASE_URL:
                    valueFrom:
                      secretKeyRef:
                        name: vehicle-pipeline-db-connection
                        key: uri
                  PAPERLESS_URL:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: PAPERLESS_URL}
                  PAPERLESS_TOKEN:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: PAPERLESS_TOKEN}
                  PAPERLESS_WEBHOOK_SECRET:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: PAPERLESS_WEBHOOK_SECRET}
                  LITELLM_BASE_URL:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: LITELLM_BASE_URL}
                  LITELLM_API_KEY:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: LITELLM_API_KEY}
                  MYGARAGE_URL: https://mygarage.apps.altus.janz.digital
                  MYGARAGE_USERNAME:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: MYGARAGE_USERNAME}
                  MYGARAGE_PASSWORD:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: MYGARAGE_PASSWORD}
                  MYGARAGE_VEHICLES:
                    valueFrom:
                      secretKeyRef: {name: vehicle-pipeline-credentials, key: MYGARAGE_VEHICLES}
                probes:
                  liveness:
                    enabled: true
                    custom: true
                    spec:
                      httpGet: {path: /healthz, port: 8000}
                      initialDelaySeconds: 10
                      periodSeconds: 10
                  readiness:
                    enabled: true
                    custom: true
                    spec:
                      httpGet: {path: /healthz, port: 8000}
                      initialDelaySeconds: 5
                      periodSeconds: 10
              authproxy:
                image:
                  repository: nginx
                  tag: "1.27"
                  pullPolicy: IfNotPresent
                command: ["nginx", "-g", "daemon off;"]
                probes:
                  liveness: {enabled: false}
                  readiness: {enabled: false}

        service:
          app:
            controller: app
            ports:
              http: {port: 8000}
          authproxy:
            controller: app
            ports:
              http: {port: 8080}

        persistence:
          nginx-conf:
            type: configMap
            name: vehicle-pipeline-nginx-basic-auth
            advancedMounts:
              app:
                authproxy:
                  - path: /etc/nginx/nginx.conf
                    subPath: nginx.conf
          basic-auth:
            type: secret
            name: vehicle-pipeline-basic-auth
            advancedMounts:
              app:
                authproxy:
                  - path: /etc/nginx/htpasswd/htpasswd
                    subPath: htpasswd

        ingress:
          app:
            enabled: true
            className: openshift-default
            annotations:
              route.openshift.io/termination: "edge"
            hosts:
              - host: vehicle-pipeline.apps.altus.janz.digital
                paths:
                  - path: /webhooks
                    service: {identifier: app, port: http}
                  - path: /healthz
                    service: {identifier: app, port: http}
                  - path: /review
                    service: {identifier: authproxy, port: http}

  project: cluster-apps
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
    automated:
      prune: true
      selfHeal: true
```

*(Verify the exact `persistence.<name>.type: configMap|secret` + `advancedMounts` keys against a real two-container example in this repo before applying — the sidecar/basic-auth wiring must match `pricing-tool-app.yaml`'s proven shape, not be freshly invented here; this file's structure is the intent, not a guaranteed-correct final form.)*

- [ ] **Step 8: kustomization.yaml**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - external-vehicle-pipeline-credentials.yaml
  - external-vehicle-pipeline-db.yaml
  - external-vehicle-pipeline-basic-auth.yaml
  - external-vehicle-pipeline-ghcr-pull.yaml
  - nginx-basic-auth-configmap.yaml
  - postgresql-database.yaml
  - vehicle-pipeline-app.yaml
```

- [ ] **Step 9: Register in bootstrap/overlays/local.home/values-apps.yaml**

Add an entry alongside the existing `mygarage`/`taxbuddy` entries (match their exact key shape — read the surrounding lines first):

```yaml
  vehicle-pipeline:
    namespace: openshift-gitops
    path: components-apps/vehicle-pipeline
```

- [ ] **Step 10: Validate manifests render**

Run: `cd home-ops && kustomize build components-apps/vehicle-pipeline`
Expected: valid YAML output, no errors (secrets won't resolve until ArgoCD applies with ESO live — this only checks kustomize syntax).

- [ ] **Step 11: Commit**

```bash
git add components-apps/vehicle-pipeline/ bootstrap/overlays/local.home/values-apps.yaml
git commit -m "feat(vehicle-pipeline): add ArgoCD deployment manifests

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 14: CI image build workflow

**Files:**
- Create: `.github/workflows/build-vehicle-pipeline-image.yaml`

**Interfaces:**
- Consumes: nothing from earlier tasks directly; builds the image `vehicle-pipeline-app.yaml` (Task 13) already references by tag `latest`.

- [ ] **Step 1: Copy and adapt trip-enricher's build workflow**

Read `.github/workflows/build-trip-enricher-image.yaml` first and mirror its trigger/build/push shape exactly, substituting the service name:

```yaml
name: Build vehicle-pipeline image

on:
  push:
    branches: [main]
    paths:
      - "containers/vehicle-pipeline/**"
  workflow_dispatch: {}

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Log in to GHCR
        run: echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u "${{ github.actor }}" --password-stdin
      - name: Build and push
        run: |
          SHORT_SHA=$(git rev-parse --short HEAD)
          docker build -f containers/vehicle-pipeline/Containerfile \
            -t ghcr.io/pixeljonas/vehicle-pipeline:sha-${SHORT_SHA} \
            -t ghcr.io/pixeljonas/vehicle-pipeline:latest \
            containers/vehicle-pipeline
          docker push --all-tags ghcr.io/pixeljonas/vehicle-pipeline
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/build-vehicle-pipeline-image.yaml
git commit -m "ci(vehicle-pipeline): add image build workflow

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 15: External provisioning (Doppler secrets + Paperless tags/workflow)

No code — this is a one-time operator runbook. Not test-driven; verification is "the value/resource exists," checked via safe read-only commands.

- [ ] **Step 1: Mint a scoped LiteLLM virtual key for this service**

Via the LiteLLM admin UI (or `/key/generate` API, per this repo's CLAUDE.md note that `model_list` isn't live-loaded but key management is a runtime API), create a new virtual key scoped to whichever model taxbuddy/honcho already uses. Note the raw key value (shown once).

- [ ] **Step 2: Create the new Doppler secrets (CLI, output suppressed — never MCP `secrets_get`/`secrets_update`)**

```bash
cd ~/projects/home-ops   # or wherever this repo's own .env.doppler-equivalent lives for the homelab/home project
source .env.doppler   # or the Doppler token bootstrap this repo actually uses
for kv in \
  "VEHICLE_PIPELINE_PAPERLESS_URL=<paperless base URL>" \
  "VEHICLE_PIPELINE_PAPERLESS_TOKEN=<new Paperless API token, see step 3>" \
  "VEHICLE_PIPELINE_PAPERLESS_WEBHOOK_SECRET=$(openssl rand -hex 32)" \
  "VEHICLE_PIPELINE_LITELLM_BASE_URL=http://litellm-app.litellm.svc:4000/v1" \
  "VEHICLE_PIPELINE_LITELLM_API_KEY=<key from step 1>" \
  "VEHICLE_PIPELINE_POSTGRES_PASSWORD=$(openssl rand -hex 24)" \
  "VEHICLE_PIPELINE_GHCR_PULL_TOKEN=<a GHCR read:packages PAT>" \
  ; do
  key="${kv%%=*}"; value="${kv#*=}"
  doppler secrets set "$key=$value" -p homelab -c home -t "$DOPPLER_TOKEN" > /dev/null 2>&1
  echo "$key rc=$?"
done
```

- [ ] **Step 2b: Basic-auth htpasswd for the review UI**

```bash
HTPASSWD=$(htpasswd -nbB jonas "<choose a password>")
doppler secrets set "VEHICLE_PIPELINE_BASIC_AUTH_HTPASSWD=$HTPASSWD" -p homelab -c home -t "$DOPPLER_TOKEN" > /dev/null 2>&1
```

- [ ] **Step 3: Create a Paperless API token for this service**

In Paperless's admin UI (Settings → API tokens, or Django admin), create a token for a dedicated or existing service account with read access to documents/tags (write access to tags only if the tag-creation part of this step is scripted rather than done via UI in Step 4). Store as `VEHICLE_PIPELINE_PAPERLESS_TOKEN` (already referenced above).

- [ ] **Step 4: Create the two vehicle auto-matching tags in Paperless**

Via Paperless's UI (Settings → Tags → Create):
- Name `Fahrzeug:ID4-auto`, Matching algorithm: **Any word**, Match: `ID.4`, Case-insensitive: yes.
- Name `Fahrzeug:Multivan-auto`, Matching algorithm: **Any word**, Match: `Multivan Multivan`, i.e. add both `Multivan` and `T7` as match words (Paperless's "any word" splits on whitespace — enter `Multivan T7`), Case-insensitive: yes.

- [ ] **Step 5: Create the Paperless Workflow**

Via Settings → Workflows → Create:
- Name: `Vehicle cost pipeline webhook`
- Trigger: **Document Added** (a second trigger for **Document Updated** is added later, when Task for #17 needs it — not required for #16's initial cutover).
- Filter: Has any of tags = `Fahrzeug:ID4-auto`, `Fahrzeug:Multivan-auto`.
- Action: **Webhook**.
  - URL: `https://vehicle-pipeline.apps.altus.janz.digital/webhooks/paperless-vehicle`
  - As JSON: yes
  - Body: use Paperless's default Jinja2 template exposing `doc_id`, `doc_title`, `doc_url`, `correspondent`, `document_type`, `added`, `owner_username` (matches `PaperlessWebhookPayload` in Task 9).
  - Headers: `X-Vehicle-Pipeline-Signature: <value of VEHICLE_PIPELINE_PAPERLESS_WEBHOOK_SECRET>`

- [ ] **Step 6: Verify end-to-end**

Upload (or re-tag) a test document in Paperless matching one of the two tags. Confirm within ~1 minute:
```bash
oc logs -n vehicle-pipeline deploy/vehicle-pipeline-app-app --tail=50 | grep -i "queued document"
```
Then visit `https://vehicle-pipeline.apps.altus.janz.digital/review` (basic-auth prompt) and confirm the draft appears.

- [ ] **Step 7: No git commit** — this task is all external system state (Doppler, Paperless, LiteLLM), nothing to commit to this repo.

---

## Self-Review Notes

- **Spec coverage:** webhook trigger (Task 9), poll backstop (Task 10), LLM extraction (Task 7), taxonomy mapping table (Task 8), draft-only review queue with edit-before-approve (Tasks 4, 11), vehicle disambiguation via tags + VIN fallback (Task 6), MyGarage REST write client for all 7 categories with live-verified schemas (Task 8), deployment on home-ops/Altus (Task 13), CI (Task 14), external Paperless/Doppler provisioning (Task 15) — all covered.
- **Explicitly out of scope for this plan** (per ticket #16 and its dependents): #17/#19's historical-backfill logic (extends this pipeline later, once it exists), #38's independent Zappi/HA ingestion component, #44/the #1787 leasing invoice (blocked on the external MyGarage fork).
- **Two open design calls are flagged, not hidden** — own namespace+DB vs. co-locating with mygarage; Postgres-as-queue vs. Redis — see Global Constraints. Worth a quick nod from Jonas before Task 13, since they're the two most annoying-to-reverse choices (namespace/DB topology; everything else here is ordinary application code).
- **No placeholders**: every task has real, runnable code except Task 15 (inherently non-code operator steps with concrete literal values) and small `*(Verify against real X before finalizing)*` notes in Task 13 confined to the two-container sidecar wiring, which depends on reading `pricing-tool-app.yaml`'s exact current syntax rather than a syntax I could confirm without another live file read — flagged explicitly rather than guessed silently.
