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
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def create_record(self, vin: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((vin, entity, dict(payload)))
        return {"id": "created-1"}


class FailingMyGarage:
    async def create_record(self, vin: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("mygarage unreachable")


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


def test_edit_unknown_item_returns_404() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.get("/review/999/edit")

    assert resp.status_code == 404
    app.dependency_overrides.clear()


def test_approve_unknown_item_returns_404_and_never_calls_mygarage() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/999/approve", data={"vin": "X"})

    assert resp.status_code == 404
    assert store.approved == []
    app.dependency_overrides.clear()


def test_reject_unknown_item_returns_404() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/999/reject")

    assert resp.status_code == 404
    assert store.rejected == []
    app.dependency_overrides.clear()


def test_edit_form_prefills_payload_values() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.get("/review/1/edit")

    assert resp.status_code == 200
    assert 'value="120.5"' in resp.text
    assert 'value="KFZ-Steuer"' in resp.text
    app.dependency_overrides.clear()


def test_approve_uses_edited_vin_for_mygarage_routing() -> None:
    """A human correcting a misidentified VIN in the review form must route
    the MyGarage write to the corrected vehicle, not the original guess."""
    store = FakeReviewStore([_item()])
    mygarage = FakeMyGarage()
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: mygarage
    client = TestClient(app)

    corrected_vin = "WV2ZZZ7HZNH000000"
    resp = client.post("/review/1/approve", data={
        "vin": corrected_vin, "date": "2026-03-01", "amount": "125.0",
        "tax_type": "KFZ-Steuer", "notes": "corrected vin",
    })

    assert resp.status_code in (200, 303)
    assert len(mygarage.calls) == 1
    called_vin, called_entity, called_payload = mygarage.calls[0]
    assert called_vin == corrected_vin
    assert called_vin != "WVGZZZE27SE017858"  # the original, pre-edit item.vin
    assert called_payload["vin"] == corrected_vin
    app.dependency_overrides.clear()


def _vinless_documents_item(item_id: int = 2) -> ReviewItem:
    """Mirrors taxonomy._build_documents's payload shape: no "vin" key,
    used when classify_vehicle couldn't identify a vehicle."""
    return ReviewItem(
        id=item_id, paperless_doc_id=43, paperless_doc_title="Unklares Dokument",
        paperless_doc_url="https://paperless.example.test/documents/43/",
        vin=None, mygarage_entity="documents", extracted_category="other",
        payload={"title": "Unklares Dokument", "document_type": "other", "description": None},
        confidence="low", status="pending", mygarage_record_id=None,
    )


def test_approve_uses_submitted_vin_for_vinless_documents_item() -> None:
    """A vin-less 'documents' draft (classify_vehicle couldn't match a
    vehicle) has no 'vin' key in its payload. The edit form renders a
    standalone vin field for this case (see review_edit.html); approving
    with a human-supplied VIN must route the MyGarage write to that VIN,
    not fall through to an empty string."""
    store = FakeReviewStore([_vinless_documents_item()])
    mygarage = FakeMyGarage()
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: mygarage
    client = TestClient(app)

    supplied_vin = "WVGZZZE27SE017858"
    resp = client.post("/review/2/approve", data={
        "vin": supplied_vin,
        "title": "Unklares Dokument", "document_type": "other", "description": "",
    })

    assert resp.status_code in (200, 303)
    assert len(mygarage.calls) == 1
    called_vin, called_entity, _called_payload = mygarage.calls[0]
    assert called_vin == supplied_vin
    assert called_entity == "documents"
    app.dependency_overrides.clear()


def test_approve_does_not_mark_approved_when_mygarage_call_fails() -> None:
    """Guards the core safety invariant: mark_approved must only be reached
    if create_record actually succeeds. A future regression (e.g. wrapping
    the call in a broad try/except) must not silently mark a failed write
    as approved."""
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_mygarage] = lambda: FailingMyGarage()
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/review/1/approve", data={
        "vin": "WVGZZZE27SE017858", "date": "2026-03-01", "amount": "125.0",
        "tax_type": "KFZ-Steuer", "notes": "corrected amount",
    })

    assert resp.status_code >= 500
    assert store.approved == []
    assert store.get(1).status == "pending"
    app.dependency_overrides.clear()
