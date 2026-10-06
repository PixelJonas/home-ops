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


def _handoff_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "paperless_doc_id": 42,
        "doc_url": "https://paperless.example.test/documents/42/",
        "vin": "WVGZZZE27SE017858",
        "category": "fuel",
        "entity": "id4",
        "payload": {"amount": "42.10", "date": "2026-09-26"},
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _handoff_client(review_store: Any, test_settings: Any) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_review_store] = lambda: review_store
    return TestClient(app)


def test_handoff_valid_signature_creates_draft(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    resp = client.post(
        "/webhooks/ingestbuddy-handoff",
        content=json.dumps(_handoff_payload()),
        headers={"X-Ingestbuddy-Signature": "handoffsecret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "review_item_id": 1}
    draft = fake_review_store.drafts[0]
    # Local sink (default): the MyGarage-shaped envelope is flattened on receipt.
    assert draft["mygarage_entity"] == "cost"
    assert draft["extracted_category"] == "fuel"
    assert draft["origin"] == "ingestbuddy"
    assert draft["payload"]["category"] == "fuel"
    assert draft["payload"]["amount_gross"] == "42.10"
    assert draft["payload"]["vin"] == _handoff_payload()["vin"]
    app.dependency_overrides.clear()


def test_handoff_mygarage_shaped_envelope_is_converted(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    body = _handoff_payload(
        category="finanzierung_zinsen", entity="documents",
        payload={"title": "Bank", "document_type": "finanzierung_zinsen", "description": "Zinsen"},
    )
    resp = client.post(
        "/webhooks/ingestbuddy-handoff", content=json.dumps(body),
        headers={"X-Ingestbuddy-Signature": "handoffsecret"},
    )
    assert resp.status_code == 200
    payload = fake_review_store.drafts[0]["payload"]
    assert payload["category"] == "financing"
    assert payload["notes"] == "Zinsen"
    app.dependency_overrides.clear()


def test_handoff_mygarage_sink_keeps_envelope_unchanged(fake_review_store: Any, test_settings: Any) -> None:
    from dataclasses import replace

    client = _handoff_client(fake_review_store, replace(test_settings, cost_sink="mygarage"))
    resp = client.post(
        "/webhooks/ingestbuddy-handoff", content=json.dumps(_handoff_payload()),
        headers={"X-Ingestbuddy-Signature": "handoffsecret"},
    )
    assert resp.status_code == 200
    assert fake_review_store.drafts[0]["mygarage_entity"] == "id4"
    assert fake_review_store.drafts[0]["payload"] == {"amount": "42.10", "date": "2026-09-26"}
    app.dependency_overrides.clear()


def test_handoff_missing_signature_is_rejected(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    resp = client.post("/webhooks/ingestbuddy-handoff", content=json.dumps(_handoff_payload()))
    assert resp.status_code == 401
    assert fake_review_store.drafts == []
    app.dependency_overrides.clear()


def test_handoff_wrong_signature_is_rejected(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    resp = client.post(
        "/webhooks/ingestbuddy-handoff",
        content=json.dumps(_handoff_payload()),
        headers={"X-Ingestbuddy-Signature": "wrong"},
    )
    assert resp.status_code == 401
    app.dependency_overrides.clear()


def test_handoff_malformed_json_is_rejected(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    resp = client.post(
        "/webhooks/ingestbuddy-handoff",
        content=b"not json",
        headers={"X-Ingestbuddy-Signature": "handoffsecret"},
    )
    assert resp.status_code == 400
    app.dependency_overrides.clear()


def test_handoff_missing_required_field_is_rejected(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    payload = _handoff_payload()
    del payload["category"]
    resp = client.post(
        "/webhooks/ingestbuddy-handoff",
        content=json.dumps(payload),
        headers={"X-Ingestbuddy-Signature": "handoffsecret"},
    )
    assert resp.status_code == 400
    app.dependency_overrides.clear()


def test_handoff_duplicate_paperless_doc_id_reuses_draft(fake_review_store: Any, test_settings: Any) -> None:
    client = _handoff_client(fake_review_store, test_settings)
    body = json.dumps(_handoff_payload())
    headers = {"X-Ingestbuddy-Signature": "handoffsecret"}
    first = client.post("/webhooks/ingestbuddy-handoff", content=body, headers=headers)
    second = client.post("/webhooks/ingestbuddy-handoff", content=body, headers=headers)
    assert first.json()["review_item_id"] == second.json()["review_item_id"]
    assert len(fake_review_store.drafts) == 1
    app.dependency_overrides.clear()
