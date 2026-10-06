"""Import a VW app trip export (trips.vw_export) into trips.imported_trips.

    python -m vehicle_pipeline.trips.import_vw <zip|dir|csv|-> [--dry-run]
    python -m vehicle_pipeline.trips.import_vw --refuel-check <vehicle>

``-`` reads the zip from stdin, so it runs inside the detector pod without
copying the file there::

    oc exec -i -n vehicle-pipeline deploy/vehicle-pipeline-app-detector -- \\
        python -m vehicle_pipeline.trips.import_vw - < ExportTrips-....zip

Needs DATABASE_URL (optional with --dry-run: then only the file is parsed).
The VIN is mapped to a vehicle via vehicle_pipeline.vehicles; an unknown
VIN aborts. Idempotent: re-importing the same or a newer, overlapping
export inserts only rows not seen before. The detector projects imported
trips on its next cycle (<= 60 s). Prints counts only.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import TextIO
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from vehicle_pipeline.trips.schema import init_trips_schema
from vehicle_pipeline.trips.store import TripStore
from vehicle_pipeline.trips.vw_export import (
    DEFAULT_TZ,
    KIND_SHORT_TERM,
    KINDS,
    ExportFormatError,
    ParsedExport,
    mask_vin,
    parse_export,
    row_hash,
)


class ImportFailed(RuntimeError):
    pass


def summarize(export: ParsedExport, tz: str, out: TextIO) -> None:
    zone = ZoneInfo(tz)
    h = export.header
    print(f"export: created {h.created_at.astimezone(zone):%Y-%m-%d %H:%M} ({tz}), "
          f"odometer {h.odometer_km} km, engine {h.engine!r}", file=out)
    for kind in KINDS:
        rows = export.by_kind(kind)
        if not rows:
            print(f"  {kind}: 0 rows", file=out)
            continue
        first, last = min(r.ended_at for r in rows), max(r.ended_at for r in rows)
        print(
            f"  {kind}: {len(rows)} rows, {first.astimezone(zone):%Y-%m-%d} .. {last.astimezone(zone):%Y-%m-%d}, "
            f"{sum(r.km for r in rows)} km, units {sorted({str(r.consumption_unit) for r in rows})}",
            file=out,
        )
    short = export.by_kind(KIND_SHORT_TERM)
    if short and h.odometer_km is not None:
        print(f"  derived odometer at the first short-term trip start: {h.odometer_km - sum(r.km for r in short)} km",
              file=out)
    if export.skipped_files:
        print(f"  skipped (unknown) files: {len(export.skipped_files)}", file=out)


def run_import(source: object, *, database_url: str | None, dry_run: bool, tz: str, out: TextIO) -> dict[str, int]:
    try:
        export = parse_export(source, tz=tz)  # type: ignore[arg-type]
    except (ExportFormatError, OSError) as exc:
        raise ImportFailed(f"cannot read export: {exc}") from exc
    summarize(export, tz, out)
    if database_url is None:
        if not dry_run:
            raise ImportFailed("DATABASE_URL is not set")
        print("dry run without DATABASE_URL: vehicle lookup and duplicate check skipped", file=out)
        return {"rows": len(export.rows)}
    pool = ConnectionPool(database_url, min_size=1, max_size=2, open=True)
    try:
        if not dry_run:
            init_trips_schema(pool)
        store = TripStore(pool)
        vehicle = store.vehicle_for_vin(export.header.vin)
        if vehicle is None:
            raise ImportFailed(
                f"the export's VIN ({mask_vin(export.header.vin)}) is not in vehicle_pipeline.vehicles; "
                "add the vehicle to the registry first"
            )
        print(f"vehicle: {vehicle}", file=out)
        if dry_run:
            hashes = [row_hash(vehicle, r) for r in export.rows]
            known = store.known_row_hashes(hashes) if _tables_exist(pool) else set()
            stats = {"rows": len(hashes), "would_insert": len(set(hashes) - known), "already_known": len(known)}
        else:
            stats = store.record_import(vehicle, export)
    finally:
        pool.close()
    print(("dry run: " if dry_run else "imported: ") + ", ".join(f"{k}={v}" for k, v in stats.items()), file=out)
    return stats


def _tables_exist(pool: ConnectionPool) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('trips.imported_trips') IS NOT NULL")
        return bool(cur.fetchone()[0])  # type: ignore[index]


def refuel_report(vehicle: str, database_url: str, tz: str, out: TextIO) -> None:
    zone = ZoneInfo(tz)
    pool = ConnectionPool(database_url, min_size=1, max_size=2, open=True)
    try:
        checks = TripStore(pool).refuel_check(vehicle)
    finally:
        pool.close()
    print("refuel_at;segment_km;odometer_from_segments;position;trip_odo_start;trip_odo_end;diff_km", file=out)
    for c in checks:
        print(
            f"{c.refuel_at.astimezone(zone):%Y-%m-%d %H:%M};{c.segment_km};{c.odometer_from_segments};{c.position};"
            f"{c.trip_odo_start};{c.trip_odo_end};{c.diff_km}",
            file=out,
        )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vehicle_pipeline.trips.import_vw", description=__doc__.split("\n")[0])
    ap.add_argument("source", nargs="?", help="export zip, extracted directory, single CSV, or - for a zip on stdin")
    ap.add_argument("--dry-run", action="store_true", help="parse (and with DATABASE_URL check), write nothing")
    ap.add_argument("--refuel-check", metavar="VEHICLE", help="print the odometer at each imported refuel")
    ap.add_argument("--tz", default=DEFAULT_TZ, help="timezone of the export timestamps (the car's local time)")
    args = ap.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL") or None
    try:
        if args.refuel_check:
            if database_url is None:
                raise ImportFailed("DATABASE_URL is not set")
            refuel_report(args.refuel_check, database_url, args.tz, sys.stdout)
            return 0
        if not args.source:
            ap.error("source is required")
        source: object = sys.stdin.buffer if args.source == "-" else args.source
        run_import(source, database_url=database_url, dry_run=args.dry_run, tz=args.tz, out=sys.stdout)
    except ImportFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
