from decimal import Decimal
from typing import get_args

import pytest

from vehicle_pipeline.cost_store import CostValidationError, build_cost_row
from vehicle_pipeline.extract import ExtractionCategory, ExtractionResult
from vehicle_pipeline.legacy_payload import to_cost_payload
from vehicle_pipeline.taxonomy import COST_CATEGORIES, EXTRACTION_TO_COST_CATEGORY, map_to_cost, map_to_mygarage

VIN = "TESTVIN0000000001"


def _result(**overrides) -> ExtractionResult:
    base = dict(category="kfz_steuer", amount=120.5, date="2026-03-01", vendor="Hauptzollamt",
                odometer_km=None, notes="Jahressteuer", confidence="high")
    base.update(overrides)
    return ExtractionResult.model_validate(base)


EXPECTED = {
    "kfz_steuer": "tax",
    "haftpflicht": "insurance",
    "kasko": "insurance",
    "hu_au": "service",
    "finanzierung_zinsen": "financing",
    "kraftstoff": "fuel",
    "reifenwechsel": "service",
    "autowaesche": "service",
    "oel_betriebsstoffe": "service",
    "adblue": "def",
    "ersatzteile": "service",
    "garantie": "other",
    "not_cost_relevant": "other",
    "other": "other",
}


def test_every_extraction_category_has_a_cost_category() -> None:
    assert set(EXPECTED) == set(get_args(ExtractionCategory))
    assert set(EXTRACTION_TO_COST_CATEGORY.values()) <= set(COST_CATEGORIES)


@pytest.mark.parametrize(("extraction_category", "cost_category"), sorted(EXPECTED.items()))
def test_map_to_cost_per_category(extraction_category: str, cost_category: str) -> None:
    draft = map_to_cost(_result(category=extraction_category, odometer_km=54000.0), VIN)
    assert draft.entity == "cost"
    assert draft.payload["category"] == cost_category
    assert draft.payload["vin"] == VIN
    assert draft.payload["amount_gross"] == 120.5
    assert draft.payload["date"] == "2026-03-01"
    assert draft.payload["vendor"] == "Hauptzollamt"
    assert draft.payload["odometer_km"] == 54000
    assert draft.payload["extra"] == {"extracted_category": extraction_category}
    # every scalar field is a valid cost row once the amount/date are there
    build_cost_row(draft.payload)


def test_map_to_cost_financing_is_not_a_document() -> None:
    """MyGarage had no financing entity (finanzierung_zinsen fell into the
    cost-less `documents`); the local sink records it as a real cost."""
    assert map_to_mygarage(_result(category="finanzierung_zinsen"), VIN).entity == "documents"
    assert map_to_cost(_result(category="finanzierung_zinsen"), VIN).payload["category"] == "financing"


@pytest.mark.parametrize(
    ("extraction_category", "amount_key", "cost_category"),
    [
        ("kfz_steuer", "amount", "tax"),
        ("haftpflicht", "premium_amount", "insurance"),
        ("hu_au", "total_cost", "service"),
        ("kraftstoff", "cost", "fuel"),
        ("adblue", "cost", "def"),
    ],
)
def test_legacy_mygarage_payloads_convert(extraction_category: str, amount_key: str, cost_category: str) -> None:
    mg = map_to_mygarage(_result(category=extraction_category, odometer_km=12345.0), VIN)
    assert amount_key in mg.payload
    flat = to_cost_payload(mg.entity, mg.payload, vin=VIN, extracted_category=extraction_category)
    assert flat["category"] == cost_category
    assert flat["amount_gross"] == 120.5
    assert flat["vin"] == VIN
    assert flat["extra"]["legacy_entity"] == mg.entity
    build_cost_row(flat)


def test_legacy_service_visit_keeps_line_items_in_extra() -> None:
    mg = map_to_mygarage(_result(category="hu_au", odometer_km=54000.0), VIN)
    flat = to_cost_payload(mg.entity, mg.payload, vin=VIN, extracted_category="hu_au")
    assert flat["odometer_km"] == 54000
    assert flat["extra"]["line_items"] == mg.payload["line_items"]
    assert flat["extra"]["service_category"] == "Inspection"


def test_legacy_insurance_uses_provider_and_start_date() -> None:
    mg = map_to_mygarage(_result(category="kasko", vendor="HUK24"), VIN)
    flat = to_cost_payload(mg.entity, mg.payload, vin=VIN, extracted_category="kasko")
    assert flat["vendor"] == "HUK24"
    assert flat["date"] == "2026-03-01"
    assert flat["extra"]["policy_type"] == "Kasko"


def test_legacy_documents_use_extracted_category_and_item_vin() -> None:
    mg = map_to_mygarage(_result(category="finanzierung_zinsen", notes="Zinsen Q3"), VIN)
    flat = to_cost_payload(mg.entity, mg.payload, vin=VIN, extracted_category="finanzierung_zinsen")
    assert flat["category"] == "financing"
    assert flat["vin"] == VIN  # documents payloads carry no vin of their own
    assert flat["notes"] == "Zinsen Q3"
    assert flat["amount_gross"] is None  # documents never had an amount: reviewer fills it in


def test_legacy_unknown_entity_falls_back_to_other() -> None:
    flat = to_cost_payload("id4", {"amount": "42.10", "date": "2026-09-26"}, vin=VIN, extracted_category="weird")
    assert flat["category"] == "other"
    assert flat["amount_gross"] == "42.10"


def test_to_cost_payload_is_identity_for_cost_items() -> None:
    payload = map_to_cost(_result(), VIN).payload
    assert to_cost_payload("cost", payload, vin="OTHER", extracted_category="kraftstoff") == payload


def test_build_cost_row_coerces_form_strings() -> None:
    row = build_cost_row({
        "category": "fuel", "date": "2026-09-01", "amount_gross": "65,40", "amount_net": "54.96",
        "vat_rate": "19", "odometer_km": "21714.0", "quantity_liters": "40.2", "vendor": " Aral ",
        "notes": "", "extra": {"a": 1},
    })
    assert row["amount_gross"] == Decimal("65.40")
    assert row["amount_net"] == Decimal("54.96")
    assert row["vat_rate"] == Decimal("19")
    assert row["odometer_km"] == 21714
    assert row["quantity_liters"] == Decimal("40.2")
    assert row["vendor"] == "Aral"
    assert row["notes"] is None
    assert row["currency"] == "EUR"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"category": "bogus", "date": "2026-01-01", "amount_gross": 1}, "category"),
        ({"category": "fuel", "date": None, "amount_gross": 1}, "date is required"),
        ({"category": "fuel", "date": "01.02.2026", "amount_gross": 1}, "YYYY-MM-DD"),
        ({"category": "fuel", "date": "2026-01-01", "amount_gross": None}, "amount_gross is required"),
        ({"category": "fuel", "date": "2026-01-01", "amount_gross": "abc"}, "amount_gross"),
        ({"category": "fuel", "date": "2026-01-01", "amount_gross": "NaN"}, "finite"),
    ],
)
def test_build_cost_row_rejects_invalid(payload: dict, message: str) -> None:
    with pytest.raises(CostValidationError, match=message):
        build_cost_row(payload)
