from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from vw_helpers import HEADER_ELECTRIC, VIN_ID4, VIN_MV, csv_text, export_zip, r

from vehicle_pipeline.trips import import_vw
from vehicle_pipeline.trips.vw_export import (
    ExportFormatError,
    kind_for_filename,
    parse_decimal,
    parse_duration,
    parse_export,
    parse_local_ts,
    row_hash,
)

D = Decimal


def test_numbers_durations() -> None:
    assert parse_decimal("12,345 km") == D("12345")
    assert parse_decimal("1,005") == D("1005")
    assert parse_decimal("6.6") == D("6.6")
    assert parse_decimal("6,6") == D("6.6")
    assert parse_decimal("12,345.5") == D("12345.5")
    assert parse_decimal("") is None and parse_decimal("-") is None
    with pytest.raises(ExportFormatError):
        parse_decimal("abc1")
    assert parse_duration("04:59") == timedelta(hours=4, minutes=59)
    assert parse_duration("99:59") == timedelta(hours=99, minutes=59)
    with pytest.raises(ExportFormatError):
        parse_duration("4h59")


def test_berlin_offsets_winter_and_summer() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    assert parse_local_ts("15/03/2026, 10:00", tz) == datetime(2026, 3, 15, 9, 0, tzinfo=UTC)  # CET
    assert parse_local_ts("30/03/2026, 10:00", tz) == datetime(2026, 3, 30, 8, 0, tzinfo=UTC)  # CEST after 29.03.
    assert parse_local_ts("24/10/2026, 10:00", tz) == datetime(2026, 10, 24, 8, 0, tzinfo=UTC)  # CEST
    assert parse_local_ts("26/10/2026, 10:00", tz) == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # CET after 25.10.


def test_parse_zip_bom_quoted_thousands_and_capped_time() -> None:
    data = export_zip(
        [r("01/10/2026, 19:10", "377", "04:12", "90", "6.9", "26.0"), r("29/09/2026, 07:00", "1,005", "99:59")],
        refuel=[r("01/10/2026, 10:00", "640", "08:00")],
        long=[r("01/10/2026, 19:10", "6,120", "99:59", "60", "7.1", "434")],
        odometer="12,345 km",
        created="02/10/2026, 07:45",
    )
    exp = parse_export(io.BytesIO(data))
    h = exp.header
    assert h.vin == VIN_MV and h.odometer_km == D("12345") and h.engine == "Combustion engine"
    assert h.created_at == datetime(2026, 10, 2, 5, 45, tzinfo=UTC)
    short = exp.by_kind("short_term")
    assert [x.km for x in short] == [D("1005"), D("377")]  # sorted by end
    last = short[-1]
    assert last.ended_at == datetime(2026, 10, 1, 17, 10, tzinfo=UTC)
    assert (last.driving_time, last.avg_speed_kmh, last.avg_consumption, last.total_consumption) == (
        timedelta(hours=4, minutes=12), D("90"), D("6.9"), D("26.0"))
    assert (last.consumption_unit, last.total_consumption_unit) == ("l/100 km", "l")
    assert short[0].driving_time_capped and not last.driving_time_capped
    assert last.raw["Trip end"] == "01/10/2026, 19:10"
    assert [len(exp.by_kind(k)) for k in ("refuel_segment", "long_term")] == [1, 1]
    assert exp.by_kind("long_term")[0].km == D("6120")


def test_electric_export_keeps_unknown_units() -> None:
    text = csv_text([r("01/10/2026, 10:00", "42", "00:40", "63", "17.9", "7.5")], vin=VIN_ID4,
                    header=HEADER_ELECTRIC, engine="Electric engine")
    exp = parse_export(_write(text))
    row = exp.rows[0]
    assert exp.header.engine == "Electric engine"
    assert (row.avg_consumption, row.consumption_unit, row.total_consumption_unit) == (D("17.9"), "kWh/100 km", "kWh")


def test_directory_and_bad_inputs(tmp_path: Path) -> None:
    folder = tmp_path / "ExportTrips-x"
    folder.mkdir()
    (folder / "Short-term data.csv").write_text(csv_text([r("01/09/2026, 10:00", "10")]), encoding="utf-8")
    (folder / "notes.csv").write_text("whatever", encoding="utf-8")
    exp = parse_export(tmp_path)
    assert len(exp.rows) == 1 and exp.skipped_files == ["notes.csv"]

    with pytest.raises(ExportFormatError, match="not a zip"):
        parse_export(b"not a zip")
    (folder / "Short-term data.csv").write_text(
        csv_text([r("01/09/2026, 10:00", "10")], header="When;How far"), encoding="utf-8")
    with pytest.raises(ExportFormatError, match="header lacks"):
        parse_export(tmp_path)


def test_row_hash_ignores_export_metadata() -> None:
    a = parse_export(io.BytesIO(export_zip([r("01/09/2026, 10:00", "10")], created="02/09/2026, 10:00")))
    b = parse_export(io.BytesIO(export_zip([r("01/09/2026, 10:00", "10")], created="09/09/2026, 10:00",
                                           odometer="11,000 km")))
    assert row_hash("multivan", a.rows[0]) == row_hash("multivan", b.rows[0])
    assert row_hash("multivan", a.rows[0]) != row_hash("id4", a.rows[0])


def test_kind_from_filename() -> None:
    assert kind_for_filename("x/Short-term data.csv") == "short_term"
    assert kind_for_filename("From refuelling.csv") == "refuel_segment"
    assert kind_for_filename("Long-term data.csv") == "long_term"
    assert kind_for_filename("other.csv") is None


def test_cli_dry_run_without_db_prints_counts_only(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    z = tmp_path / "e.zip"
    z.write_bytes(export_zip([r("01/09/2026, 10:00", "10"), r("02/09/2026, 10:00", "20")]))
    assert import_vw.main([str(z), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "short_term: 2 rows" in out and "30 km" in out and "10270 km" in out  # 10,300 - 30
    assert VIN_MV not in out
    assert import_vw.main([str(z)]) == 2  # a real import needs DATABASE_URL
    (tmp_path / "bad.zip").write_bytes(b"nope")
    assert import_vw.main([str(tmp_path / "bad.zip"), "--dry-run"]) == 2


def _write(text: str) -> io.BytesIO:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("E/Short-term data.csv", text.encode("utf-8"))
    buf.seek(0)
    return buf
