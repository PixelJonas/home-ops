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
    # OR missed_fillup=True is passed -- confirmed live 2026-09-12
    # backfilling real Multivan fuel receipts, none of which print an
    # odometer reading (ordinary gas-station receipts never do). This
    # field was already extracted by the LLM (ExtractionResult.odometer_km)
    # but never included in this payload. missed_fillup's own description
    # ("Skipped recording a fill-up") is the correct semantic fit: it
    # tells MyGarage's fuel-economy calculations not to expect this
    # record to participate in a consecutive-fillup chain.
    return MyGarageDraft("fuel", {
        "vin": vin, "date": e.date, "cost": e.amount, "notes": e.notes,
        "odometer_km": e.odometer_km,
        "missed_fillup": e.odometer_km is None,
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
