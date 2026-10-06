"""Synthetic VW app trip exports for the import tests. Everything here is
made up: fake VINs (TESTVIN...), invented trips and odometers."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

VIN_MV = "TESTVIN000000MV01"
VIN_ID4 = "TESTVIN00000ID401"
VIN_UNKNOWN = "TESTVIN0000000X99"

HEADER_COMBUSTION = (
    "Trip end;Distance covered in km;Driving time in hrs;Average speed in km/h;"
    "Average fuel consumption in l/100 km;Total consumption in l"
)
HEADER_ELECTRIC = (
    "Trip end;Distance covered in km;Driving time in hrs;Average speed in km/h;"
    "Average electric consumption in kWh/100 km;Total consumption in kWh"
)

# (trip end local "dd/mm/yyyy, HH:MM", km, driving time "H:MM", speed, avg consumption, total)
Row = tuple[str, str, str, str, str, str]


def _q(value: str) -> str:
    return f'"{value}"' if "," in value else value


def csv_text(
    rows: Sequence[Row],
    *,
    vin: str = VIN_MV,
    odometer: str = "10,300 km",
    created: str = "20/09/2026, 22:00",
    engine: str = "Combustion engine",
    header: str = HEADER_COMBUSTION,
) -> str:
    lines = [
        f'{vin};;;Distance:;"{odometer}";Export created on:;"{created}"',
        f";;;;;;;{engine}",
        header,
    ]
    lines += [";".join([f'"{end}"', _q(km), t, sp, avg, tot]) for end, km, t, sp, avg, tot in rows]
    return "\ufeff" + "\r\n".join(lines)


def export_zip(
    short: Sequence[Row],
    refuel: Sequence[Row] = (),
    long: Sequence[Row] = (),
    *,
    folder: str = "ExportTrips-2026-09-20 22-00-00",
    **kw: str,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, rows in (("Short-term data.csv", short), ("From refuelling.csv", refuel), ("Long-term data.csv", long)):
            zf.writestr(f"{folder}/{name}", csv_text(rows, **kw).encode("utf-8"))
    return buf.getvalue()


def r(end: str, km: str, t: str = "01:00", speed: str = "50", avg: str = "7.0", total: str = "7.0") -> Row:
    return (end, km, t, speed, avg, total)


# Scenario used by projection and integration tests (Berlin = UTC+2 in
# Aug/Sep). Export A (20.09.) knows T1..T3, anchor 10,300; export B
# (25.09.) repeats T2/T3 and adds T4, anchor 10,420.
T1 = r("01/08/2026, 12:00", "100", "01:30")  # 10:00Z, 10000 -> 10100
T2 = r("10/09/2026, 09:00", "50", "00:45")  # 07:00Z, 10100 -> 10150
T3 = r("15/09/2026, 18:00", "150", "02:00")  # 16:00Z, 10150 -> 10300
T4 = r("22/09/2026, 08:00", "120", "01:10")  # 06:00Z, 10300 -> 10420
EXPORT_A = {"short": [T1, T2, T3], "odometer": "10,300 km", "created": "20/09/2026, 22:00"}
EXPORT_B = {"short": [T2, T3, T4], "odometer": "10,420 km", "created": "25/09/2026, 12:00"}


def scenario_zip(spec: dict, **kw: object) -> bytes:
    return export_zip(spec["short"], odometer=spec["odometer"], created=spec["created"], **kw)  # type: ignore[arg-type]


def inputs_from(*zips: bytes, vehicle: str = "multivan", checks: dict | None = None):  # type: ignore[no-untyped-def]
    """In-memory equivalent of TripStore.record_import + load_import_inputs:
    export ids 1.., one row per row_hash linked to every export holding it.
    All kinds are kept (refuel_crosscheck needs them)."""
    from vehicle_pipeline.trips.imports import ImportedRow, ImportExport, ImportInputs
    from vehicle_pipeline.trips.vw_export import parse_export, row_hash

    exports = []
    rows: dict[str, list] = {}
    for i, z in enumerate(zips, start=1):
        p = parse_export(io.BytesIO(z))
        exports.append(ImportExport(i, "vw_export", vehicle, p.header.created_at, p.header.odometer_km))
        for row in p.rows:
            entry = rows.setdefault(row_hash(vehicle, row), [len(rows) + 1, row, []])
            entry[2].append(i)
    return ImportInputs(
        exports=exports,
        rows=[
            ImportedRow(rid, "vw_export", vehicle, row.kind, row.ended_at, row.km, row.driving_time, tuple(eids))
            for rid, row, eids in rows.values()
        ],
        checks=checks or {},
    )
