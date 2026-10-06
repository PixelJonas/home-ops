#!/usr/bin/env python3
"""Generate idempotent seed SQL for vehicle_pipeline.costs from a MyGarage
CSV export (one ``public.<table>.csv`` per table, as written by
``\\copy ... TO ... CSV HEADER``).

    gen_mygarage_seed.py EXPORT_DIR [-o OUT.sql]

Emits one cost row per MyGarage record:

* fuel_records       -> fuel (or charging if only kWh is set)
* def_records        -> def
* service_visits     -> service, ONE row per visit; its service_line_items
                        go into ``extra.line_items`` (not separate rows)
* financing_records  -> financing
* tax_records        -> tax
* insurance_policy_vehicles (+ insurance_policies) -> insurance

Every row gets ``source='mygarage_seed'`` and
``source_ref='<table>:<id>'`` and is inserted with
``ON CONFLICT (source, source_ref) DO NOTHING``, so applying the output
twice is a no-op. Vehicles are resolved by VIN against
``vehicle_pipeline.vehicles`` (populated by the app at startup); a VIN
with no vehicle aborts the whole transaction instead of skipping rows.

The output contains VINs and personal cost data: never commit it.
Counts per category are printed to stderr; row contents never are.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SOURCE = "mygarage_seed"
COLUMNS = (
    "vehicle_id", "date", "category", "amount_gross", "vat_amount", "currency",
    "odometer_km", "quantity_liters", "quantity_kwh", "vendor",
    "source", "source_ref", "extra", "notes",
)
# Columns never copied into `extra` (mapped elsewhere or pure bookkeeping).
_SKIP_EXTRA = {"id", "vin", "vendor_id", "visit_id", "policy_id", "created_at", "updated_at"}


def read_table(export_dir: Path, table: str) -> list[dict[str, str]]:
    path = export_dir / f"public.{table}.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_rows(export_dir: Path) -> list[dict[str, Any]]:
    vendors = {v["id"]: v.get("name") or None for v in read_table(export_dir, "vendors")}
    rows: list[dict[str, Any]] = []

    for r in read_table(export_dir, "fuel_records"):
        liters, kwh = _num(r.get("liters")), _num(r.get("kwh"))
        rows.append(_row(
            "fuel_records", r,
            category="charging" if kwh is not None and liters is None else "fuel",
            amount=r.get("cost"),
            vendor=_s(r.get("station_name_freetext")),
            quantity_liters=liters,
            quantity_kwh=kwh,
            mapped={"date", "cost", "odometer_km", "liters", "kwh", "station_name_freetext", "notes"},
        ))

    for r in read_table(export_dir, "def_records"):
        rows.append(_row(
            "def_records", r, category="def", amount=r.get("cost"),
            quantity_liters=_num(r.get("liters")),
            mapped={"date", "cost", "odometer_km", "liters", "notes"},
        ))

    line_items: dict[str, list[dict[str, Any]]] = {}
    for li in read_table(export_dir, "service_line_items"):
        line_items.setdefault(li["visit_id"], []).append(_compact(li, skip=_SKIP_EXTRA))
    for r in read_table(export_dir, "service_visits"):
        items = line_items.get(r["id"], [])
        amount = r.get("total_cost")
        if _num(amount) is None:
            amount = str(sum((_num(i.get("cost")) or Decimal(0)) for i in items))
        extra_items = {"line_items": items} if items else {}
        rows.append(_row(
            "service_visits", r, category="service", amount=amount,
            vendor=vendors.get(r.get("vendor_id") or ""),
            vat_amount=_num(r.get("tax_amount")),
            mapped={"date", "total_cost", "odometer_km", "tax_amount", "notes"},
            extra=extra_items,
        ))

    for r in read_table(export_dir, "financing_records"):
        rows.append(_row(
            "financing_records", r, category="financing", amount=r.get("amount"),
            vendor=vendors.get(r.get("vendor_id") or ""),
            mapped={"date", "amount", "notes"},
            extra={"financing_category": r.get("category")} if r.get("category") else {},
            drop={"category"},
        ))

    for r in read_table(export_dir, "tax_records"):
        rows.append(_row(
            "tax_records", r, category="tax", amount=r.get("amount"),
            mapped={"date", "amount", "notes"},
        ))

    policies = {p["id"]: p for p in read_table(export_dir, "insurance_policies")}
    for link in read_table(export_dir, "insurance_policy_vehicles"):
        policy = policies.get(link.get("policy_id") or "")
        if policy is None:
            continue
        amount = link.get("premium_share")
        if _num(amount) is None:
            amount = policy.get("premium_amount")
        merged = {**link, "date": policy.get("start_date", "")}
        rows.append(_row(
            "insurance_policy_vehicles", merged, category="insurance", amount=amount,
            vendor=_s(policy.get("provider")),
            mapped={"date", "premium_share", "notes"},
            extra={"policy": _compact(policy, skip=_SKIP_EXTRA | {"provider", "notes"})},
        ))

    return rows


def _row(
    table: str,
    r: dict[str, str],
    *,
    category: str,
    amount: str | None,
    mapped: set[str],
    vendor: str | None = None,
    quantity_liters: Decimal | None = None,
    quantity_kwh: Decimal | None = None,
    vat_amount: Decimal | None = None,
    extra: dict[str, Any] | None = None,
    drop: set[str] = frozenset(),  # type: ignore[assignment]
) -> dict[str, Any]:
    gross = _num(amount)
    if gross is None:
        raise SystemExit(f"{table}:{r.get('id')}: no amount, refusing to seed a cost without one")
    odometer = _num(r.get("odometer_km"))
    full_extra = {"mygarage_table": table, **_compact(r, skip=_SKIP_EXTRA | mapped | drop), **(extra or {})}
    return {
        "vin": r["vin"],
        "date": (r.get("date") or "")[:10],
        "category": category,
        "amount_gross": gross,
        "vat_amount": vat_amount,
        "currency": "EUR",
        "odometer_km": int(odometer) if odometer is not None else None,
        "quantity_liters": quantity_liters,
        "quantity_kwh": quantity_kwh,
        "vendor": vendor,
        "source": SOURCE,
        "source_ref": f"{table}:{r['id']}",
        "extra": full_extra,
        "notes": _s(r.get("notes")),
    }


def render_sql(rows: list[dict[str, Any]]) -> str:
    out = [
        "-- Generated by containers/vehicle-pipeline/scripts/gen_mygarage_seed.py.",
        "-- Contains personal data: do not commit. Idempotent: safe to apply twice.",
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
    ]
    for vin in sorted({r["vin"] for r in rows}):
        out.append(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM vehicle_pipeline.vehicles WHERE vin = "
            f"{_lit(vin)}) THEN RAISE EXCEPTION 'seed: no vehicle_pipeline.vehicles row for a VIN in this "
            "seed -- start the app with the vehicle config first'; END IF; END $$;"
        )
    for r in rows:
        values = [f"(SELECT id FROM vehicle_pipeline.vehicles WHERE vin = {_lit(r['vin'])})"]
        values += [_lit(r[c]) for c in COLUMNS[1:]]
        values[COLUMNS.index("category")] += "::vehicle_pipeline.cost_category"
        values[COLUMNS.index("date")] += "::date"
        out.append(
            f"INSERT INTO vehicle_pipeline.costs ({', '.join(COLUMNS)})\n"
            f"VALUES ({', '.join(values)})\n"
            "ON CONFLICT (source, source_ref) DO NOTHING;"
        )
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def _lit(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return _quote(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)) + "::jsonb"
    return _quote(str(value))


def _quote(text: str) -> str:
    # standard_conforming_strings is on (default since PG 9.1): only ' needs doubling.
    return "'" + text.replace("'", "''") + "'"


def _num(value: str | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def _s(value: str | None) -> str | None:
    return value.strip() or None if value else None


def _compact(r: dict[str, str], *, skip: set[str]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if k not in skip and v not in (None, "")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="write SQL here (default: stdout)")
    args = parser.parse_args(argv)

    rows = build_rows(args.export_dir)
    sql = render_sql(rows)
    if args.output:
        args.output.write_text(sql, encoding="utf-8")
    else:
        sys.stdout.write(sql)

    counts = Counter(r["category"] for r in rows)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"{len(rows)} cost row(s): {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
