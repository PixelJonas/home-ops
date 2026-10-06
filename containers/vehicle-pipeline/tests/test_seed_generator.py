import importlib.util
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from seed_fixture import VIN_A, write_export

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_mygarage_seed.py"
_spec = importlib.util.spec_from_file_location("gen_mygarage_seed", _SCRIPT)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_build_rows_one_cost_per_record_and_visit(tmp_path: Path) -> None:
    rows = gen.build_rows(write_export(tmp_path))
    assert Counter(r["category"] for r in rows) == {"fuel": 1, "charging": 1, "def": 1, "service": 2, "financing": 1}
    assert len({r["source_ref"] for r in rows}) == len(rows)
    assert all(r["source"] == "mygarage_seed" for r in rows)


def test_service_visit_line_items_go_into_extra(tmp_path: Path) -> None:
    rows = {r["source_ref"]: r for r in gen.build_rows(write_export(tmp_path))}
    visit = rows["service_visits:30"]
    assert visit["amount_gross"] == Decimal("250.00")
    assert visit["vendor"] == "Werkstatt O'Brien"
    assert visit["odometer_km"] == 1500
    assert [li["description"] for li in visit["extra"]["line_items"]] == ["Öl", "Filter"]
    assert visit["extra"]["service_category"] == "Maintenance"
    # a visit without total_cost falls back to the sum of its line items
    assert rows["service_visits:31"]["amount_gross"] == Decimal("12.50")


def test_fuel_charging_def_financing_fields(tmp_path: Path) -> None:
    rows = {r["source_ref"]: r for r in gen.build_rows(write_export(tmp_path))}
    fuel = rows["fuel_records:10"]
    assert (fuel["quantity_liters"], fuel["odometer_km"], fuel["notes"]) == (Decimal("40.5"), 1000, "voll")
    assert fuel["extra"]["is_full_tank"] == "t"
    charge = rows["fuel_records:11"]
    assert (charge["category"], charge["quantity_kwh"], charge["vendor"]) == ("charging", Decimal("55.2"), "Wallbox")
    assert rows["def_records:20"]["quantity_liters"] == Decimal("10")
    fin = rows["financing_records:40"]
    assert fin["extra"]["financing_category"] == "loan_payment"
    assert fin["amount_gross"] == Decimal("399.00")


def test_render_sql_is_idempotent_and_maps_vin_via_subselect(tmp_path: Path) -> None:
    sql = gen.render_sql(gen.build_rows(write_export(tmp_path)))
    assert sql.count("INSERT INTO vehicle_pipeline.costs") == 6
    assert sql.count("ON CONFLICT (source, source_ref) DO NOTHING;") == 6
    assert f"(SELECT id FROM vehicle_pipeline.vehicles WHERE vin = '{VIN_A}')" in sql
    assert "'Werkstatt O''Brien'" in sql  # quotes escaped
    assert sql.startswith("-- Generated") and "BEGIN;" in sql and sql.rstrip().endswith("COMMIT;")
    # every jsonb literal is valid JSON
    for literal in re.findall(r"'((?:[^']|'')*)'::jsonb", sql):
        json.loads(literal.replace("''", "'"))


def test_missing_amount_aborts(tmp_path: Path) -> None:
    export = write_export(tmp_path)
    (export / "public.tax_records.csv").write_text("id,vin,date,tax_type,amount\n1,X,2026-01-01,KFZ,\n")
    with pytest.raises(SystemExit):
        gen.build_rows(export)


def test_cli_writes_file_and_prints_counts_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "seed.sql"
    assert gen.main([str(write_export(tmp_path / "export")), "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "6 cost row(s)" in err
    assert VIN_A not in err
    assert out.read_text().count("INSERT INTO") == 6
