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
            "line_items": [],
        })
    return _build


def _build_fuel(e: ExtractionResult, vin: str) -> MyGarageDraft:
    return MyGarageDraft("fuel", {
        "vin": vin, "date": e.date, "cost": e.amount, "notes": e.notes,
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
    "kraftstoff": _build_fuel,
    "adblue": _build_def,
    "garantie": _build_warranty,
}
