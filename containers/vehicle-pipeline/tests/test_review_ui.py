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
