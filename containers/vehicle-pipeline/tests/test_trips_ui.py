from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vehicle_pipeline.main import app
from vehicle_pipeline.trips.store import AnnotationError, EffectiveTrip, validate_annotation
from vehicle_pipeline.trips_ui import get_trip_store, get_vollkosten_loader, month_bounds


def _trip(key: str, business: bool | None = None, purpose: str | None = None) -> EffectiveTrip:
    return EffectiveTrip(
        trip_key=key,
        vehicle_id="id4",
        driver="alice",
        started_at=datetime(2026, 10, 1, 7, 0, tzinfo=UTC),
        ended_at=datetime(2026, 10, 1, 7, 40, tzinfo=UTC),
        km=None,
        gps_km=Decimal("31.2"),
        status="odometer_pending",
        source="ha_phone",
        business=business,
        business_via="notify" if business is not None else None,
        purpose=purpose,
        detected_vehicle_id="id4",
        detected_driver="alice",
    )


class FakeTripStore:
    def __init__(self, trips: list[EffectiveTrip]) -> None:
        self.trips = {t.trip_key: t for t in trips}
        self.calls: list[tuple[Any, dict[str, Any], str, str | None]] = []
        self.list_kwargs: dict[str, Any] = {}

    def list_effective(self, **kw: Any) -> list[EffectiveTrip]:
        self.list_kwargs = kw
        return list(self.trips.values())

    def get_effective(self, key: str) -> EffectiveTrip | None:
        return self.trips.get(key)

    def vehicle_ids(self) -> list[str]:
        return ["id4", "multivan"]

    def annotate(self, trip_keys: Any, *, via: str, actor: str | None = None, **values: Any) -> int:
        for field, value in values.items():
            validate_annotation(field, value)
        self.calls.append((trip_keys, values, via, actor))
        return 1


@pytest.fixture
def client_store() -> Any:
    store = FakeTripStore([_trip("span:1"), _trip("span:2", business=True, purpose="Kunde")])
    app.dependency_overrides[get_trip_store] = lambda: store
    yield TestClient(app), store
    app.dependency_overrides.clear()


def test_trips_list_renders_rows_and_filters(client_store: Any) -> None:
    client, store = client_store
    resp = client.get("/trips", params={"vehicle": "id4", "month": "2026-10", "unflagged": "1", "status": "odometer_pending"})
    assert resp.status_code == 200
    assert "span:1" in resp.text and "Kunde" in resp.text and "31.2" in resp.text
    kw = store.list_kwargs
    assert kw["vehicle"] == "id4" and kw["unflagged_only"] is True and kw["status"] == "odometer_pending"
    assert (kw["month_start"], kw["month_end"]) == month_bounds("2026-10")


def test_trips_deep_link_shows_single_trip(client_store: Any) -> None:
    client, _ = client_store
    resp = client.get("/trips", params={"trip": "span:2"})
    assert resp.status_code == 200
    assert 'value="span:2" checked' in resp.text and 'value="span:1"' not in resp.text


def test_trips_rejects_bad_filters(client_store: Any) -> None:
    client, _ = client_store
    assert client.get("/trips", params={"status": "bogus"}).status_code == 422
    assert client.get("/trips", params={"month": "Oktober"}).status_code == 422


def test_bulk_annotate_business_private_clear(client_store: Any) -> None:
    client, store = client_store
    for action, expected in (("business", True), ("private", False), ("clear", None)):
        resp = client.post(
            "/trips/annotate",
            data={"trip_key": ["span:1", "span:2"], "action": action, "back": "/trips?month=2026-10"},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/trips?month=2026-10"
        assert store.calls[-1] == (["span:1", "span:2"], {"business": expected}, "ui", "ui")


def test_bulk_annotate_without_selection_writes_nothing(client_store: Any) -> None:
    client, store = client_store
    resp = client.post("/trips/annotate", data={"action": "business"}, follow_redirects=False)
    assert resp.status_code == 303 and store.calls == []
    assert client.post("/trips/annotate", data={"trip_key": "span:1", "action": "maybe"}).status_code == 422


def test_bulk_annotate_never_redirects_offsite(client_store: Any) -> None:
    client, _ = client_store
    for back in ("https://evil.test/", "//evil.test", "/trips//evil", "/review"):
        resp = client.post("/trips/annotate", data={"action": "private", "back": back}, follow_redirects=False)
        assert resp.headers["location"] == "/trips"


def test_purpose_edit(client_store: Any) -> None:
    client, store = client_store
    resp = client.post("/trips/purpose", data={"trip_key": "span:1", "purpose": " Baustelle "}, follow_redirects=False)
    assert resp.status_code == 303
    assert store.calls[-1] == ("span:1", {"purpose": " Baustelle "}, "ui", "ui")
    client.post("/trips/purpose", data={"trip_key": "span:1", "purpose": ""}, follow_redirects=False)
    assert store.calls[-1][1] == {"purpose": None}
    assert client.post("/trips/purpose", data={"trip_key": "span:1", "purpose": "x" * 600}).status_code == 422


def test_validate_annotation() -> None:
    validate_annotation("business", False)
    validate_annotation("driver", None)
    for field, value in (("business", "yes"), ("purpose", "  "), ("colour", "red"), ("vehicle", 3)):
        with pytest.raises(AnnotationError):
            validate_annotation(field, value)


def test_vollkosten_page_renders_rows() -> None:
    row = {
        "vehicle_id": "id4",
        "period_start": datetime(2026, 1, 1).date(),
        "cost_total": Decimal("1200.00"),
        "cost_financing": Decimal("200.00"),
        "cost_excl_financing": Decimal("1000.00"),
        **{f"cost_{c}": Decimal("0") for c in ("fuel", "charging", "service", "insurance", "tax", "def", "parking", "toll", "other")},
        "km": Decimal("1000.0"),
        "km_business": Decimal("250.0"),
        "km_gps_unconfirmed": Decimal("12.0"),
        "cost_per_km": Decimal("1.2000"),
        "cost_per_km_excl_financing": Decimal("1.0000"),
        "business_share": Decimal("0.2500"),
        "business_cost": Decimal("300.00"),
        "business_cost_excl_financing": Decimal("250.00"),
        "trips_total": 10,
        "trips_km_unknown": 1,
        "trips_unflagged": 0,
        "complete": False,
    }
    seen: list[tuple[str, str | None]] = []

    def loader(period: str, vehicle: str | None) -> list[dict[str, Any]]:
        seen.append((period, vehicle))
        return [row]

    app.dependency_overrides[get_vollkosten_loader] = lambda: loader
    try:
        client = TestClient(app)
        resp = client.get("/vollkosten")
        assert resp.status_code == 200
        assert "300.00" in resp.text and "vorläufig" in resp.text and ">2026<" in resp.text
        assert client.get("/vollkosten", params={"period": "monthly", "vehicle": "id4"}).status_code == 200
        assert seen == [("yearly", None), ("monthly", "id4")]
        assert client.get("/vollkosten", params={"period": "weekly"}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_review_list_links_trips_pages() -> None:
    from vehicle_pipeline.review_ui import get_review_store

    class Empty:
        def list_pending(self) -> list[Any]:
            return []

    app.dependency_overrides[get_review_store] = lambda: Empty()
    try:
        text = TestClient(app).get("/review").text
        assert 'href="/trips"' in text and 'href="/vollkosten"' in text
    finally:
        app.dependency_overrides.clear()
