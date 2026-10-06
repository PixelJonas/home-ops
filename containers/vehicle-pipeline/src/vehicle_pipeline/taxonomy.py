from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vehicle_pipeline.extract import ExtractionResult


COST_ENTITY = "cost"
"""review_items.mygarage_entity value for drafts in the flat local-cost
shape (map_to_cost). Every other value is a legacy MyGarage entity type."""

COST_CATEGORIES = (
    "fuel", "charging", "service", "insurance", "tax",
    "financing", "def", "parking", "toll", "other",
)
"""Mirrors the vehicle_pipeline.cost_category enum in db.py."""

EXTRACTION_TO_COST_CATEGORY: dict[str, str] = {
    "kfz_steuer": "tax",
    "haftpflicht": "insurance",
    "kasko": "insurance",
    "hu_au": "service",
    "reifenwechsel": "service",
    "autowaesche": "service",
    "oel_betriebsstoffe": "service",
    "ersatzteile": "service",
    "finanzierung_zinsen": "financing",
    "kraftstoff": "fuel",
    "adblue": "def",
    "garantie": "other",
    "not_cost_relevant": "other",
    "other": "other",
}


@dataclass(frozen=True)
class CostDraft:
    entity: str
    payload: dict[str, Any]


def map_to_cost(extraction: ExtractionResult, vin: str) -> CostDraft:
    """Flat local-cost draft: one scalar field per editable cost column
    (rendered 1:1 by review_edit.html) plus a non-editable ``extra`` dict.
    Fuel is always ``fuel`` here -- the reviewer switches it to
    ``charging`` for an EV receipt (the extractor has no such category)."""
    category = EXTRACTION_TO_COST_CATEGORY.get(extraction.category, "other")
    odometer = int(extraction.odometer_km) if extraction.odometer_km is not None else None
    return CostDraft(COST_ENTITY, {
        "vin": vin,
        "category": category,
        "date": extraction.date,
        "amount_gross": extraction.amount,
        "amount_net": None,
        "vat_rate": None,
        "vendor": extraction.vendor,
        "odometer_km": odometer,
        "quantity_liters": None,
        "quantity_kwh": None,
        "notes": extraction.notes,
        "extra": {"extracted_category": extraction.category},
    })


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
            # MyGarage's tax-deduction PDF report (backend/app/routes/
            # reports.py's download_tax_deduction_pdf) reads ONLY
            # service_visit.line_items[].cost -- it never looks at
            # total_cost at all. Confirmed live 2026-09-12 by fetching that
            # handler's source directly: with an empty line_items list (the
            # prior version of this code), a real, correctly-costed
            # service-visits record would still be entirely invisible to
            # the one report this whole pipeline exists to feed. One line
            # item carrying the same amount is required, not optional
            # polish -- total_cost alone only feeds MyGarage's general
            # cost views, not this report.
            "line_items": [{"description": e.notes or e.vendor or category, "cost": e.amount}],
        })
    return _build


def _build_fuel(e: ExtractionResult, vin: str) -> MyGarageDraft:
    # MyGarage rejects a fuel record with a 422 unless odometer_km is set
    # -- confirmed live 2026-09-12 backfilling real Multivan fuel receipts,
    # none of which print an odometer reading (ordinary gas-station
    # receipts never do). This field was already extracted by the LLM
    # (ExtractionResult.odometer_km) but never included in this payload.
    #
    # missed_fillup is NOT a valid bypass here -- confirmed by the API's
    # own error text ("set missed_fillup=True only if you ALSO can't
    # supply a fuel amount") and by testing it live: a record with real
    # liters/cost data still 422s with missed_fillup=True and no
    # odometer_km. An earlier version of this fix wrongly auto-set that
    # flag whenever odometer_km was null, which does not work and was
    # never actually exercised against the live API before merging (the
    # unit test mocked the transport). The real answer: leave odometer_km
    # null in the draft when the LLM couldn't find one, and let Jonas
    # supply the real value in review_edit.html's already-generic
    # per-payload-key form before approving -- he's the one who can look
    # up the car's actual mileage at fill-up time, the pipeline can't.
    return MyGarageDraft("fuel", {
        "vin": vin, "date": e.date, "cost": e.amount, "notes": e.notes,
        "odometer_km": e.odometer_km,
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
    # Confirmed live 2026-09-12: a real Reifenreparatur invoice landed with
    # a real amount (41.60 EUR) but the generic `documents` fallback has no
    # cost field at all -- MyGarage's `documents` entity is upload-only
    # metadata (title/document_type/description), so the cost was silently
    # invisible to any cost total. This is NOT the financing/fork gap
    # (docs/research/mygarage-financing-cost-gap-fork-vs-sink.md) -- that's
    # specific to loan/lease principal+interest. service-visits already has
    # a total_cost field and needs no fork; ticket #16's own taxonomy table
    # explicitly listed "service-visits line-item or documents" as the two
    # options for this category and only the cost-less one got implemented.
    "ersatzteile": _build_service_visit("Maintenance"),
    "kraftstoff": _build_fuel,
    "adblue": _build_def,
    "garantie": _build_warranty,
}
