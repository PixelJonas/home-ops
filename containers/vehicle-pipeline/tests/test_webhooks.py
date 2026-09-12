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
