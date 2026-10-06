"""Parser for the VW app's in-car trip export ("Export trips"): a zip with
one folder holding three ``;``-separated CSVs, UTF-8 with BOM.

Every CSV starts with the same two preamble lines, then a header::

    <VIN>;;;Distance:;"12,345 km";Export created on:;"02/10/2026, 07:45"
    ;;;;;;;Combustion engine
    Trip end;Distance covered in km;Driving time in hrs;Average speed in km/h;...

* ``Short-term data.csv`` -- one row per trip (VW merges trips separated by
  a stop under 2 h; driving time excludes the stops) -> kind ``short_term``.
* ``From refuelling.csv`` -- one row per refuel: the segment since the
  previous refuel, row time = refuel moment -> kind ``refuel_segment``.
* ``Long-term data.csv`` -- cumulative counters -> kind ``long_term``.

Timestamps are dd/mm/yyyy, HH:MM local time (TZ, default Europe/Berlin);
numbers use an English thousands comma (``"1,005"``); driving time is
H:MM and saturates at ``99:59``. Columns are matched by header name, so
an EV export (kWh/100 km, other engine line) parses too: the consumption
unit is taken from the header and stored as-is, unknown columns are kept
in ``raw``.

Pure parsing only; the VIN is returned for the vehicle lookup and must
never be stored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

SOURCE_VW_EXPORT = "vw_export"
KIND_SHORT_TERM = "short_term"
KIND_REFUEL = "refuel_segment"
KIND_LONG_TERM = "long_term"
KINDS = (KIND_SHORT_TERM, KIND_REFUEL, KIND_LONG_TERM)
DEFAULT_TZ = "Europe/Berlin"
# The in-car counter saturates here; a trip showing it drove at least this long.
DRIVING_TIME_CAP = timedelta(hours=99, minutes=59)

_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+$")
_TS_FORMATS = ("%d/%m/%Y, %H:%M", "%d/%m/%Y %H:%M", "%d.%m.%Y, %H:%M", "%d.%m.%Y %H:%M")


class ExportFormatError(ValueError):
    pass


@dataclass(frozen=True)
class ExportHeader:
    vin: str
    odometer_km: Decimal | None
    created_at: datetime  # UTC
    engine: str | None


@dataclass(frozen=True)
class ExportRow:
    kind: str
    ended_at: datetime  # UTC
    km: Decimal
    driving_time: timedelta | None
    avg_speed_kmh: Decimal | None
    avg_consumption: Decimal | None
    consumption_unit: str | None
    total_consumption: Decimal | None
    total_consumption_unit: str | None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def driving_time_capped(self) -> bool:
        return self.driving_time is not None and self.driving_time >= DRIVING_TIME_CAP


@dataclass
class ParsedExport:
    header: ExportHeader
    rows: list[ExportRow]
    skipped_files: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[ExportRow]:
        return [r for r in self.rows if r.kind == kind]


# ------------------------------------------------------------------ values


def parse_decimal(value: str | None) -> Decimal | None:
    """``"12,345 km"`` -> 12345, ``"1,005"`` -> 1005, ``"6.6"`` -> 6.6,
    ``"6,6"`` (decimal comma) -> 6.6, empty / ``-`` -> None."""
    if value is None:
        return None
    s = value.strip().replace("\u00a0", " ")
    if not s or s in {"-", "--"}:
        return None
    m = re.fullmatch(r"(-?[\d.,]+)\s*(?:[A-Za-z/%][A-Za-z0-9/% .]*)?", s)  # optional trailing unit
    if not m:
        raise ExportFormatError(f"not a number: {value!r}")
    s = m[1]
    if "," in s and "." in s or _THOUSANDS.match(s):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ExportFormatError(f"not a number: {value!r}") from exc


def parse_duration(value: str | None) -> timedelta | None:
    """``"04:59"`` / ``"99:59"`` (hours may exceed 24) -> timedelta."""
    if value is None or not value.strip():
        return None
    m = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", value.strip())
    if not m:
        raise ExportFormatError(f"not a driving time: {value!r}")
    return timedelta(hours=int(m[1]), minutes=int(m[2]), seconds=int(m[3] or 0))


def parse_local_ts(value: str, tz: ZoneInfo) -> datetime:
    """``"01/10/2026, 19:10"`` (local wall time) -> aware UTC datetime. In
    the repeated hour of the autumn DST switch the earlier instant wins."""
    s = value.strip()
    for fmt in _TS_FORMATS:
        try:
            naive = datetime.strptime(s, fmt)  # noqa: DTZ007 - local wall time, zone attached below
        except ValueError:
            continue
        return naive.replace(tzinfo=tz).astimezone(UTC)
    raise ExportFormatError(f"not a timestamp: {value!r}")


def _unit(header: str) -> str | None:
    m = re.search(r"\bin\s+(.+)$", header.strip(), flags=re.IGNORECASE)
    return m[1].strip() if m else None


# -------------------------------------------------------------------- CSV


def kind_for_filename(name: str) -> str | None:
    n = Path(name).name.lower()
    if "short-term" in n or "short term" in n:
        return KIND_SHORT_TERM
    if "refuel" in n or "recharg" in n or "charging" in n:
        return KIND_REFUEL
    if "long-term" in n or "long term" in n:
        return KIND_LONG_TERM
    return None


def _parse_preamble(line1: list[str], line2: list[str], tz: ZoneInfo) -> ExportHeader:
    vin = (line1[0] if line1 else "").strip()
    if not vin:
        raise ExportFormatError("first line has no VIN")
    odometer: Decimal | None = None
    created: datetime | None = None
    for i, cell in enumerate(line1[:-1]):
        label = cell.strip().lower()
        nxt = line1[i + 1]
        if label.startswith("distance"):
            odometer = parse_decimal(nxt)
        elif label.startswith("export created"):
            created = parse_local_ts(nxt, tz)
    if created is None:
        raise ExportFormatError("first line has no 'Export created on:' timestamp")
    engine = next((c.strip() for c in reversed(line2) if c.strip()), None)
    return ExportHeader(vin=vin, odometer_km=odometer, created_at=created, engine=engine)


def _column_map(header: list[str]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        name = h.strip().lower()
        if name.startswith("trip end"):
            cols.setdefault("ended_at", i)
        elif name.startswith("distance"):
            cols.setdefault("km", i)
        elif name.startswith("driving time"):
            cols.setdefault("driving_time", i)
        elif name.startswith("average speed"):
            cols.setdefault("avg_speed_kmh", i)
        elif name.startswith("average") and "consumption" in name:
            cols.setdefault("avg_consumption", i)
        elif name.startswith("total consumption"):
            cols.setdefault("total_consumption", i)
    missing = [c for c in ("ended_at", "km") if c not in cols]
    if missing:
        raise ExportFormatError(f"header lacks column(s) {missing}: {header!r}")
    return cols


def parse_csv(text: str, kind: str, tz: ZoneInfo) -> tuple[ExportHeader, list[ExportRow]]:
    lines = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
    if len(lines) < 3:
        raise ExportFormatError("file has fewer than 3 lines")
    header = _parse_preamble(lines[0], lines[1], tz)
    names = [h.strip() for h in lines[2]]
    cols = _column_map(names)
    units = {
        "consumption_unit": _unit(names[cols["avg_consumption"]]) if "avg_consumption" in cols else None,
        "total_consumption_unit": _unit(names[cols["total_consumption"]]) if "total_consumption" in cols else None,
    }

    def cell(row: list[str], key: str) -> str | None:
        i = cols.get(key)
        return row[i] if i is not None and i < len(row) else None

    rows: list[ExportRow] = []
    for n, row in enumerate(lines[3:], start=4):
        if not any(c.strip() for c in row):
            continue
        try:
            km = parse_decimal(cell(row, "km"))
            if km is None:
                raise ExportFormatError("empty distance")
            rows.append(
                ExportRow(
                    kind=kind,
                    ended_at=parse_local_ts(cell(row, "ended_at") or "", tz),
                    km=km,
                    driving_time=parse_duration(cell(row, "driving_time")),
                    avg_speed_kmh=parse_decimal(cell(row, "avg_speed_kmh")),
                    avg_consumption=parse_decimal(cell(row, "avg_consumption")),
                    total_consumption=parse_decimal(cell(row, "total_consumption")),
                    raw={names[i]: v for i, v in enumerate(row) if i < len(names) and names[i]},
                    **units,
                )
            )
        except ExportFormatError as exc:
            raise ExportFormatError(f"{kind} line {n}: {exc}") from exc
    return header, rows


def _read_files(source: str | Path | bytes | BinaryIO) -> list[tuple[str, bytes]]:
    """(name, bytes) of every CSV in a zip (path, bytes or binary stream),
    a directory (recursive) or a single CSV path."""
    if isinstance(source, (bytes, bytearray)):
        return _read_zip(io.BytesIO(source))
    if hasattr(source, "read"):
        return _read_zip(io.BytesIO(source.read()))  # type: ignore[union-attr]
    path = Path(source)  # type: ignore[arg-type]
    if path.is_dir():
        return [(str(p.relative_to(path)), p.read_bytes()) for p in sorted(path.rglob("*.csv"))]
    if path.suffix.lower() == ".csv":
        return [(path.name, path.read_bytes())]
    with path.open("rb") as f:
        return _read_zip(io.BytesIO(f.read()))


def _read_zip(buf: io.BytesIO) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(buf)
    except zipfile.BadZipFile as exc:
        raise ExportFormatError("input is not a zip file") from exc
    with zf:
        return [
            (i.filename, zf.read(i))
            for i in sorted(zf.infolist(), key=lambda i: i.filename)
            if not i.is_dir() and i.filename.lower().endswith(".csv") and "__macosx" not in i.filename.lower()
        ]


def parse_export(source: str | Path | bytes | BinaryIO, tz: str = DEFAULT_TZ) -> ParsedExport:
    zone = ZoneInfo(tz)
    header: ExportHeader | None = None
    rows: list[ExportRow] = []
    skipped: list[str] = []
    for name, data in _read_files(source):
        kind = kind_for_filename(name)
        if kind is None:
            skipped.append(Path(name).name)
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExportFormatError(f"{Path(name).name}: not UTF-8") from exc
        h, r = parse_csv(text, kind, zone)
        if header is None:
            header = h
        elif (h.vin, h.created_at) != (header.vin, header.created_at):
            raise ExportFormatError("the CSVs of one export disagree on vehicle or export time")
        rows.extend(r)
    if header is None:
        raise ExportFormatError("no trip CSV (short-term / refuelling / long-term) found")
    rows.sort(key=lambda r: (KINDS.index(r.kind), r.ended_at))
    return ParsedExport(header=header, rows=rows, skipped_files=skipped)


# ------------------------------------------------------------------- hash


def _num(d: Decimal | None) -> str | None:
    return None if d is None else format(d.normalize(), "f")


def row_hash(vehicle_id: str, row: ExportRow, source: str = SOURCE_VW_EXPORT) -> str:
    """Identity of an exported row: the same trip in a later, overlapping
    export hashes the same, so re-imports are idempotent. Export metadata
    is deliberately not part of it."""
    payload = [
        source,
        vehicle_id,
        row.kind,
        row.ended_at.astimezone(UTC).isoformat(),
        _num(row.km),
        None if row.driving_time is None else int(row.driving_time.total_seconds()),
        _num(row.avg_speed_kmh),
        _num(row.avg_consumption),
        row.consumption_unit,
        _num(row.total_consumption),
        row.total_consumption_unit,
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def mask_vin(vin: str) -> str:
    return f"...{vin[-4:]}" if len(vin) > 4 else "..."
