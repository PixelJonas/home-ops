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
    assert draft.payload["total_cost"] == 120.5


def test_service_visit_line_item_carries_the_cost() -> None:
    """MyGarage's tax-deduction PDF report reads only
    service_visit.line_items[].cost, never total_cost -- confirmed live
    2026-09-12 against the real report handler's source. A service-visits
    draft with an empty line_items list would be entirely invisible to
    that report despite total_cost being correctly set."""
    draft = map_to_mygarage(
        _result(category="hu_au", amount=89.0, notes="TÜV Hauptuntersuchung"), VIN
    )
    assert draft.payload["line_items"] == [{"description": "TÜV Hauptuntersuchung", "cost": 89.0}]


def test_service_visit_line_item_falls_back_to_vendor_then_category_for_description() -> None:
    draft = map_to_mygarage(_result(category="hu_au", notes=None, vendor="TÜV Nord"), VIN)
    assert draft.payload["line_items"][0]["description"] == "TÜV Nord"

    draft = map_to_mygarage(_result(category="hu_au", notes=None, vendor=None), VIN)
    assert draft.payload["line_items"][0]["description"] == "Inspection"


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


def test_ersatzteile_maps_to_service_visits_with_cost() -> None:
    """Confirmed live 2026-09-12: a real Reifenreparatur invoice (41.60 EUR)
    landed in the cost-less `documents` fallback before this fix -- the
    amount must reach a cost-bearing entity."""
    draft = map_to_mygarage(_result(category="ersatzteile", amount=41.60), VIN)
    assert draft.entity == "service-visits"
    assert draft.payload["service_category"] == "Maintenance"
    assert draft.payload["total_cost"] == 41.60


def test_unmapped_category_falls_back_to_documents() -> None:
    draft = map_to_mygarage(_result(category="finanzierung_zinsen"), VIN)
    assert draft.entity == "documents"


def test_not_cost_relevant_falls_back_to_documents() -> None:
    draft = map_to_mygarage(_result(category="not_cost_relevant", amount=None), VIN)
    assert draft.entity == "documents"
