"""Synthetic MyGarage CSV export (no real data) for the seed-generator tests."""

from __future__ import annotations

import csv
from pathlib import Path

VIN_A = "TESTVIN0000000001"
VIN_B = "TESTVIN0000000002"

TABLES: dict[str, list[dict[str, str]]] = {
    "vendors": [{"id": "1", "name": "Werkstatt O'Brien", "city": ""}],
    "fuel_records": [
        {"id": "10", "vin": VIN_A, "date": "2026-01-05", "odometer_km": "1000.0", "liters": "40.5",
         "kwh": "", "cost": "70.10", "is_full_tank": "t", "station_name_freetext": "", "notes": "voll"},
        {"id": "11", "vin": VIN_B, "date": "2026-01-06", "odometer_km": "", "liters": "",
         "kwh": "55.2", "cost": "20.00", "is_full_tank": "", "station_name_freetext": "Wallbox", "notes": ""},
    ],
    "def_records": [
        {"id": "20", "vin": VIN_A, "date": "2026-02-01", "odometer_km": "", "liters": "10",
         "cost": "15.99", "brand": "AdBlue", "notes": ""},
    ],
    "service_visits": [
        {"id": "30", "vin": VIN_A, "vendor_id": "1", "date": "2026-03-01", "odometer_km": "1500",
         "total_cost": "250.00", "tax_amount": "", "notes": "Inspektion", "service_category": "Maintenance"},
        {"id": "31", "vin": VIN_A, "vendor_id": "", "date": "2026-03-15", "odometer_km": "",
         "total_cost": "", "tax_amount": "", "notes": "", "service_category": "Detailing"},
    ],
    "service_line_items": [
        {"id": "300", "visit_id": "30", "description": "Öl", "cost": "100.00", "is_inspection": "f"},
        {"id": "301", "visit_id": "30", "description": "Filter", "cost": "150.00", "is_inspection": "f"},
        {"id": "310", "visit_id": "31", "description": "Wäsche", "cost": "12.50", "is_inspection": "f"},
    ],
    "financing_records": [
        {"id": "40", "vin": VIN_B, "vendor_id": "1", "date": "2026-04-01", "amount": "399.00",
         "category": "loan_payment", "notes": ""},
    ],
    "tax_records": [],
    "insurance_policies": [],
    "insurance_policy_vehicles": [],
}


def write_export(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for table, rows in TABLES.items():
        path = directory / f"public.{table}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            if not rows:
                fh.write("id\n")
                continue
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return directory
