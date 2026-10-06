"""Convert MyGarage-shaped draft payloads into the flat local-cost shape.

Two producers still emit MyGarage-shaped drafts: review items created
before the local cost sink existed (still `pending` in review_items), and
ingestbuddy's VehicleSink hand-off, whose taxonomy is a verbatim port of
taxonomy.map_to_mygarage. Both go through ``to_cost_payload`` so the
review UI and cost_store only ever deal with one shape.
"""

from __future__ import annotations

from typing import Any

from vehicle_pipeline.taxonomy import COST_CATEGORIES, COST_ENTITY, EXTRACTION_TO_COST_CATEGORY

ENTITY_TO_COST_CATEGORY: dict[str, str] = {
    "fuel": "fuel",
    "def": "def",
    "service-visits": "service",
    "insurance": "insurance",
    "tax-records": "tax",
    "warranties": "other",
    "documents": "other",
}

_AMOUNT_KEYS = ("amount_gross", "cost", "total_cost", "premium_amount", "amount")
_DATE_KEYS = ("date", "start_date")
_VENDOR_KEYS = ("vendor", "provider")
_NOTES_KEYS = ("notes", "description")
# MyGarage-specific detail worth keeping, but not a cost column.
_EXTRA_KEYS = (
    "line_items", "service_category", "policy_type", "policy_number", "end_date",
    "tax_type", "warranty_type", "title", "document_type", "liters",
)


def to_cost_payload(
    entity: str,
    payload: dict[str, Any],
    *,
    vin: str | None = None,
    extracted_category: str | None = None,
) -> dict[str, Any]:
    """Return a flat cost payload (same keys as taxonomy.map_to_cost).

    Already-flat payloads (entity == "cost") are returned unchanged, so this
    is safe to call on any review item.
    """
    if entity == COST_ENTITY:
        return dict(payload)

    category = _category(entity, extracted_category)
    extra: dict[str, Any] = {"legacy_entity": entity}
    if extracted_category:
        extra["extracted_category"] = extracted_category
    for key in _EXTRA_KEYS:
        value = payload.get(key)
        if value not in (None, "", []):
            extra[key] = value

    liters = payload.get("liters")
    odometer = payload.get("odometer_km")
    return {
        "vin": payload.get("vin") or vin or "",
        "category": category,
        "date": _first(payload, _DATE_KEYS),
        "amount_gross": _first(payload, _AMOUNT_KEYS),
        "amount_net": None,
        "vat_rate": None,
        "vendor": _first(payload, _VENDOR_KEYS),
        "odometer_km": int(float(odometer)) if _is_number(odometer) else None,
        "quantity_liters": liters if category in ("fuel", "def") and _is_number(liters) else None,
        "quantity_kwh": None,
        "notes": _first(payload, _NOTES_KEYS),
        "extra": extra,
    }


def _category(entity: str, extracted_category: str | None) -> str:
    # The extraction category is the more specific signal: it is what
    # recovers financing (finanzierung_zinsen), which MyGarage had no
    # entity for and dumped into `documents`.
    if extracted_category in COST_CATEGORIES and extracted_category != "other":
        return extracted_category
    if extracted_category in EXTRACTION_TO_COST_CATEGORY:
        mapped = EXTRACTION_TO_COST_CATEGORY[extracted_category]
        if mapped != "other" or entity not in ENTITY_TO_COST_CATEGORY:
            return mapped
    return ENTITY_TO_COST_CATEGORY.get(entity, "other")


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _is_number(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
