from typing import Any

from fastapi.testclient import TestClient

from vehicle_pipeline.main import app
from vehicle_pipeline.review_store import ReviewItem
from vehicle_pipeline.review_ui import get_cost_sink, get_cost_store, get_mygarage, get_review_store


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
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.get("/review")
    assert resp.status_code == 200
    assert "KFZ-Steuer 2026" in resp.text
    app.dependency_overrides.clear()


def test_approve_commits_to_mygarage_and_marks_approved() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
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
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/1/reject")

    assert resp.status_code in (200, 303)
    assert store.rejected == [1]
    app.dependency_overrides.clear()


def test_edit_unknown_item_returns_404() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.get("/review/999/edit")

    assert resp.status_code == 404
    app.dependency_overrides.clear()


def test_approve_unknown_item_returns_404_and_never_calls_mygarage() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/999/approve", data={"vin": "X"})

    assert resp.status_code == 404
    assert store.approved == []
    app.dependency_overrides.clear()


def test_reject_unknown_item_returns_404() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
    app.dependency_overrides[get_mygarage] = lambda: FakeMyGarage()
    client = TestClient(app)

    resp = client.post("/review/999/reject")

    assert resp.status_code == 404
    assert store.rejected == []
    app.dependency_overrides.clear()


def test_edit_form_prefills_payload_values() -> None:
    store = FakeReviewStore([_item()])
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
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
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
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
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
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
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
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


# --- COST_SINK=local (default) -------------------------------------------

from vehicle_pipeline.cost_store import (  # noqa: E402
    ReviewItemNotPendingError,
    UnknownVehicleError,
    build_cost_row,
)

KNOWN_VEHICLES = {"TESTVIN0000000001": "id4"}


class FakeCostStore:
    """Mirrors PostgresCostStore.insert_from_review's contract: validates,
    resolves the vehicle (VIN or slug), and only then marks the review item
    approved -- all-or-nothing."""

    def __init__(self, review_store: FakeReviewStore) -> None:
        self._review_store = review_store
        self.inserted: list[tuple[int, dict[str, Any]]] = []

    def insert_from_review(self, item: ReviewItem, payload: dict[str, Any]) -> int:
        build_cost_row(payload)
        ref = payload.get("vin") or item.vin
        if ref not in KNOWN_VEHICLES and ref not in KNOWN_VEHICLES.values():
            raise UnknownVehicleError(f"unknown vehicle {ref!r}")
        if self._review_store.get(item.id).status != "pending":
            raise ReviewItemNotPendingError("not pending")
        cost_id = len(self.inserted) + 100
        self.inserted.append((item.id, dict(payload)))
        self._review_store.mark_approved(item.id, str(cost_id))
        return cost_id


def _cost_item(item_id: int = 5, vin: str | None = "TESTVIN0000000001") -> ReviewItem:
    return ReviewItem(
        id=item_id, paperless_doc_id=50, paperless_doc_title="Tankquittung",
        paperless_doc_url="https://paperless.example.test/documents/50/",
        vin=vin, mygarage_entity="cost", extracted_category="kraftstoff",
        payload={"vin": vin or "", "category": "fuel", "date": "2026-09-01", "amount_gross": 65.0,
                 "amount_net": None, "vat_rate": None, "vendor": "Tankstelle", "odometer_km": None,
                 "quantity_liters": None, "quantity_kwh": None, "notes": None,
                 "extra": {"extracted_category": "kraftstoff"}},
        confidence="high", status="pending", mygarage_record_id=None,
    )


def _local_client(store: FakeReviewStore, mygarage: Any = None) -> tuple[TestClient, FakeCostStore]:
    cost_store = FakeCostStore(store)
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_store] = lambda: cost_store
    app.dependency_overrides[get_cost_sink] = lambda: "local"
    app.dependency_overrides[get_mygarage] = lambda: mygarage
    return TestClient(app), cost_store


def test_local_approve_inserts_cost_and_never_calls_mygarage() -> None:
    store = FakeReviewStore([_cost_item()])
    mygarage = FakeMyGarage()
    client, cost_store = _local_client(store, mygarage)

    resp = client.post("/review/5/approve", data={
        "vin": "TESTVIN0000000001", "category": "fuel", "date": "2026-09-01",
        "amount_gross": "66.5", "odometer_km": "21714", "quantity_liters": "40.2",
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert mygarage.calls == []
    (item_id, payload), = cost_store.inserted
    assert item_id == 5
    assert payload["amount_gross"] == 66.5
    assert payload["odometer_km"] == "21714"
    assert payload["quantity_liters"] == "40.2"
    assert payload["extra"] == {"extracted_category": "kraftstoff"}
    assert store.approved == [(5, "100")]
    app.dependency_overrides.clear()


def test_local_approve_accepts_vehicle_slug() -> None:
    store = FakeReviewStore([_cost_item(vin=None)])
    client, cost_store = _local_client(store)

    resp = client.post("/review/5/approve", data={"vin": "id4"}, follow_redirects=False)

    assert resp.status_code == 303
    assert cost_store.inserted[0][1]["vin"] == "id4"
    app.dependency_overrides.clear()


def test_local_approve_unknown_vin_returns_422_and_stays_pending() -> None:
    store = FakeReviewStore([_cost_item()])
    client, cost_store = _local_client(store)

    resp = client.post("/review/5/approve", data={"vin": "NOSUCHVIN00000000"})

    assert resp.status_code == 422
    assert "unknown vehicle" in resp.text
    assert cost_store.inserted == []
    assert store.approved == []
    assert store.updated_payloads == []
    assert store.get(5).status == "pending"
    app.dependency_overrides.clear()


def test_local_approve_invalid_amount_returns_422() -> None:
    store = FakeReviewStore([_cost_item()])
    client, cost_store = _local_client(store)

    resp = client.post("/review/5/approve", data={"amount_gross": "zwölf"})

    assert resp.status_code == 422
    assert "amount_gross" in resp.text
    assert store.get(5).status == "pending"
    app.dependency_overrides.clear()


def test_local_approve_already_approved_returns_409() -> None:
    store = FakeReviewStore([_cost_item()])
    client, cost_store = _local_client(store)
    store.mark_approved(5, "x")
    store.approved.clear()

    resp = client.post("/review/5/approve", data={})

    assert resp.status_code == 409
    assert cost_store.inserted == []
    app.dependency_overrides.clear()


def test_local_approve_converts_legacy_mygarage_item() -> None:
    """Pending items created before the local sink are MyGarage-shaped
    (here: tax-records). Approving them with COST_SINK=local must flatten
    them into a cost, not send them to MyGarage."""
    legacy = ReviewItem(**{**_item().__dict__, "vin": "TESTVIN0000000001",
                           "payload": {**_item().payload, "vin": "TESTVIN0000000001"}})
    store = FakeReviewStore([legacy])
    mygarage = FakeMyGarage()
    client, cost_store = _local_client(store, mygarage)

    resp = client.post("/review/1/approve", data={"amount_gross": "125.0"}, follow_redirects=False)

    assert resp.status_code == 303
    assert mygarage.calls == []
    payload = cost_store.inserted[0][1]
    assert payload["category"] == "tax"
    assert payload["amount_gross"] == 125.0
    assert payload["date"] == "2026-03-01"
    assert payload["extra"]["legacy_entity"] == "tax-records"
    app.dependency_overrides.clear()


def test_local_edit_form_shows_cost_fields_and_category_select() -> None:
    store = FakeReviewStore([_item()])
    client, _ = _local_client(store)

    resp = client.get("/review/1/edit")

    assert resp.status_code == 200
    assert 'name="amount_gross" value="120.5"' in resp.text
    assert '<option value="tax" selected>' in resp.text
    assert "Kosten buchen" in resp.text
    assert "MyGarage" not in resp.text
    app.dependency_overrides.clear()


def test_mygarage_sink_refuses_local_cost_items() -> None:
    store = FakeReviewStore([_cost_item()])
    mygarage = FakeMyGarage()
    app.dependency_overrides[get_review_store] = lambda: store
    app.dependency_overrides[get_cost_sink] = lambda: "mygarage"
    app.dependency_overrides[get_mygarage] = lambda: mygarage
    client = TestClient(app)

    resp = client.post("/review/5/approve", data={})

    assert resp.status_code == 422
    assert mygarage.calls == []
    app.dependency_overrides.clear()
