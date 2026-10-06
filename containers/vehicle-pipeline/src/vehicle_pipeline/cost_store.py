"""Local cost sink: turns an approved review item into a row in
vehicle_pipeline.costs (replaces MyGarage as the system of record)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from vehicle_pipeline.review_store import ReviewItem
from vehicle_pipeline.taxonomy import COST_CATEGORIES, COST_ENTITY


class CostValidationError(ValueError):
    """The reviewed payload can't become a cost row (HTTP 422)."""


class UnknownVehicleError(CostValidationError):
    """No vehicles row matches the submitted VIN/slug (HTTP 422)."""


class ReviewItemNotPendingError(RuntimeError):
    """The review item was already approved/rejected concurrently (HTTP 409)."""


class CostStore(Protocol):
    def insert_from_review(self, item: ReviewItem, payload: dict[str, Any]) -> int: ...


def build_cost_row(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce a flat cost payload (taxonomy.map_to_cost shape,
    values possibly strings from the HTML form) into typed column values.
    The vehicle is resolved separately (needs the DB)."""
    category = _text(payload.get("category"))
    if category not in COST_CATEGORIES:
        raise CostValidationError(f"category must be one of {', '.join(COST_CATEGORIES)}, got {category!r}")

    raw_date = _text(payload.get("date"))
    if raw_date is None:
        raise CostValidationError("date is required")
    try:
        cost_date = date.fromisoformat(raw_date[:10])
    except ValueError as exc:
        raise CostValidationError(f"date must be YYYY-MM-DD, got {raw_date!r}") from exc

    amount_gross = _decimal(payload.get("amount_gross"), "amount_gross")
    if amount_gross is None:
        raise CostValidationError("amount_gross is required")

    odometer = _decimal(payload.get("odometer_km"), "odometer_km")
    extra = payload.get("extra")
    if extra is not None and not isinstance(extra, dict):
        extra = {"raw": str(extra)}

    return {
        "date": cost_date,
        "category": category,
        "amount_gross": amount_gross,
        "amount_net": _decimal(payload.get("amount_net"), "amount_net"),
        "vat_amount": _decimal(payload.get("vat_amount"), "vat_amount"),
        "vat_rate": _decimal(payload.get("vat_rate"), "vat_rate"),
        "currency": _text(payload.get("currency")) or "EUR",
        "odometer_km": int(odometer) if odometer is not None else None,
        "quantity_liters": _decimal(payload.get("quantity_liters"), "quantity_liters"),
        "quantity_kwh": _decimal(payload.get("quantity_kwh"), "quantity_kwh"),
        "vendor": _text(payload.get("vendor")),
        "notes": _text(payload.get("notes")),
        "extra": extra,
    }


class PostgresCostStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def insert_from_review(self, item: ReviewItem, payload: dict[str, Any]) -> int:
        """Insert the cost row and mark the review item approved in ONE
        transaction: either both happen or neither does, so a failure can
        never leave an approved item without its cost (or a cost whose
        item is still pending and could be approved twice)."""
        row = build_cost_row(payload)
        vehicle_ref = _text(payload.get("vin")) or item.vin
        if not vehicle_ref:
            raise UnknownVehicleError("no vehicle given")

        source = "ingestbuddy" if item.origin == "ingestbuddy" else "paperless"
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            # Lock the review item first so two concurrent approvals of the
            # same item serialize here instead of racing to the INSERT.
            cur.execute(
                "SELECT status FROM vehicle_pipeline.review_items WHERE id = %s FOR UPDATE",
                (item.id,),
            )
            current = cur.fetchone()
            if current is None or current[0] != "pending":
                raise ReviewItemNotPendingError(f"review item {item.id} is no longer pending")

            cur.execute(
                "SELECT id FROM vehicle_pipeline.vehicles WHERE vin = %s OR id = %s",
                (vehicle_ref, vehicle_ref),
            )
            found = cur.fetchone()
            if found is None:
                raise UnknownVehicleError(f"unknown vehicle {vehicle_ref!r}")
            vehicle_id = found[0]

            cur.execute(
                """
                INSERT INTO vehicle_pipeline.costs
                    (vehicle_id, date, category, amount_gross, amount_net, vat_amount, vat_rate,
                     currency, odometer_km, quantity_liters, quantity_kwh, vendor,
                     paperless_doc_id, review_item_id, source, source_ref, extra, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    vehicle_id, row["date"], row["category"], row["amount_gross"], row["amount_net"],
                    row["vat_amount"], row["vat_rate"], row["currency"], row["odometer_km"],
                    row["quantity_liters"], row["quantity_kwh"], row["vendor"],
                    item.paperless_doc_id, item.id, source, f"review_item:{item.id}",
                    Jsonb(row["extra"]) if row["extra"] is not None else None, row["notes"],
                ),
            )
            inserted = cur.fetchone()
            assert inserted is not None
            cost_id = int(inserted[0])

            cur.execute(
                """
                UPDATE vehicle_pipeline.review_items
                SET status = 'approved', sink_record_id = %s, payload = %s,
                    mygarage_entity = %s, vin = %s, reviewed_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (str(cost_id), Jsonb(payload), COST_ENTITY, vehicle_ref, item.id),
            )
            if cur.rowcount != 1:
                # Raising inside conn.transaction() rolls back the INSERT too.
                raise ReviewItemNotPendingError(f"review item {item.id} is no longer pending")
            return cost_id


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any, field: str) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        result = Decimal(text.replace(",", ".")) if isinstance(value, str) else Decimal(str(value))
    except InvalidOperation as exc:
        raise CostValidationError(f"{field} must be a number, got {value!r}") from exc
    if not result.is_finite():
        raise CostValidationError(f"{field} must be a finite number, got {value!r}")
    return result
