"""Postgres access for the trip detector (``trips`` schema)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from vehicle_pipeline.trips.imports import (
    END_SLACK,
    SOURCE_VW,
    AnchorCheck,
    ImportedRow,
    ImportExport,
    ImportInputs,
    RefuelCheck,
    anchor_points,
    refuel_crosscheck,
)
from vehicle_pipeline.trips.models import (
    DETECTOR_VERSION,
    SOURCE_PHONE,
    DetectedSpan,
    Position,
    ProjectionResult,
    Reading,
    ReadingIn,
    Span,
    Trip,
)
from vehicle_pipeline.trips.signals import CLOSE_AFTER
from vehicle_pipeline.trips.util import parse_ts
from vehicle_pipeline.trips.vw_export import KIND_REFUEL, KIND_SHORT_TERM, ParsedExport, row_hash

ANNOTATION_FIELDS = ("business", "purpose", "driver", "vehicle")
ANNOTATION_VIAS = ("notify", "ui", "api", "import")
TRIP_STATUSES = ("ok", "odometer_split", "odometer_pending", "odometer_stale")


class AnnotationError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveTrip:
    """A row of trips.trips_effective: the projection with the latest
    annotation per field applied."""

    trip_key: str
    vehicle_id: str | None
    driver: str | None
    started_at: datetime
    ended_at: datetime | None
    km: Decimal | None
    gps_km: Decimal | None
    status: str
    source: str
    business: bool | None
    business_via: str | None
    purpose: str | None
    detected_vehicle_id: str | None
    detected_driver: str | None


_EFFECTIVE_COLS = (
    "trip_key, vehicle_id, driver, started_at, ended_at, km, gps_km, status, source, business, business_via, "
    "purpose, detected_vehicle_id, detected_driver"
)


def validate_annotation(field: str, value: Any) -> None:
    """None clears a field (stored as JSON null, so the clear is itself on
    the append-only record)."""
    if field not in ANNOTATION_FIELDS:
        raise AnnotationError(f"unknown annotation field {field!r}")
    if value is None:
        return
    if field == "business":
        if not isinstance(value, bool):
            raise AnnotationError("business must be true, false or null")
    elif not isinstance(value, str) or not value.strip():
        raise AnnotationError(f"{field} must be a non-empty string or null")
    elif len(value) > 500:
        raise AnnotationError(f"{field} is too long")


# How far before the projection window raw spans/positions are loaded, so
# a cluster straddling the window start is still seen whole.
LOAD_MARGIN = timedelta(days=1)


class TripStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------ cursors

    def get_cursor(self, entity_id: str) -> datetime | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT last_ts FROM trips.cursors WHERE entity_id = %s", (entity_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_cursor(self, entity_id: str, ts: datetime) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trips.cursors (entity_id, last_ts) VALUES (%s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET last_ts = GREATEST(trips.cursors.last_ts, EXCLUDED.last_ts)
                """,
                (entity_id, ts),
            )

    # ----------------------------------------------------------- readings

    def insert_readings(self, readings: Iterable[ReadingIn]) -> int:
        inserted = 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            for r in readings:
                cur.execute(
                    """
                    INSERT INTO trips.odometer_readings
                        (vehicle_id, km, data_captured_at, ha_last_changed, source)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (vehicle_id, data_captured_at, km, source) DO NOTHING
                    """,
                    (r.vehicle_id, r.km, r.data_captured_at, r.ha_last_changed, r.source),
                )
                inserted += max(cur.rowcount, 0)
        return inserted

    # -------------------------------------------------------------- spans

    def record_spans(
        self, person: str, detected: Sequence[DetectedSpan], window_start: datetime, window_end: datetime
    ) -> dict[str, int]:
        """Upsert one person's spans detected over [window_start,
        window_end]. A detected span that overlaps (or is within
        CLOSE_AFTER of) a stored one extends it -- windows overlap and can
        start mid-drive, so the stored row keeps the earliest start and its
        id (= the trip key anchor). Stored open spans this window should
        have seen but did not are closed at their last-seen time."""
        stats = {"inserted": 0, "extended": 0, "closed_unseen": 0}
        touched: set[int] = set()
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            # Coverage gap (detector down longer than the window overlap):
            # an open span last seen before this window cannot be extended
            # reliably -- close it at its last supporting signal first so it
            # cannot swallow unrelated later drives.
            stats["closed_unseen"] += self._close_open_spans(cur, person, before=window_start, seen_before=window_start)
            for d in sorted(detected, key=lambda s: s.started_at):
                d_end = d.ended_at or window_end
                evidence = {**d.evidence, "last_seen_at": window_end.isoformat()}
                cur.execute(
                    """
                    SELECT id, started_at, ended_at, vehicle_id, evidence FROM trips.drive_spans
                    WHERE person = %s AND source = %s
                      AND started_at <= %s
                      AND COALESCE(ended_at, 'infinity'::timestamptz) >= %s
                    ORDER BY started_at LIMIT 1 FOR UPDATE
                    """,
                    (person, SOURCE_PHONE, d_end + CLOSE_AFTER, d.started_at - CLOSE_AFTER),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO trips.drive_spans
                            (person, vehicle_id, started_at, ended_at, evidence, source, detector_version)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (person, started_at, source) DO UPDATE SET
                            ended_at = EXCLUDED.ended_at,
                            vehicle_id = COALESCE(trips.drive_spans.vehicle_id, EXCLUDED.vehicle_id),
                            evidence = EXCLUDED.evidence, updated_at = now()
                        RETURNING id
                        """,
                        (person, d.vehicle_id, d.started_at, d.ended_at, Jsonb(evidence), SOURCE_PHONE, DETECTOR_VERSION),
                    )
                    touched.add(cur.fetchone()[0])  # type: ignore[index]
                    stats["inserted"] += 1
                    continue
                span_id, started, ended, vehicle, old_ev = row
                new_end = None if d.ended_at is None else (d.ended_at if ended is None else max(ended, d.ended_at))
                cur.execute(
                    """
                    UPDATE trips.drive_spans
                    SET started_at = %s, ended_at = %s, vehicle_id = %s, evidence = %s,
                        detector_version = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        min(started, d.started_at),
                        new_end,
                        vehicle or d.vehicle_id,
                        Jsonb(merge_span_evidence(old_ev or {}, evidence)),
                        DETECTOR_VERSION,
                        span_id,
                    ),
                )
                touched.add(span_id)
                stats["extended"] += 1

            # Open spans that started before this window but were not
            # re-detected in it: their signals ended before the window.
            stats["closed_unseen"] += self._close_open_spans(cur, person, before=window_start, skip=touched)
        return stats

    @staticmethod
    def _close_open_spans(
        cur: Any, person: str, *, before: datetime, seen_before: datetime | None = None, skip: set[int] | None = None
    ) -> int:
        cur.execute(
            """
            SELECT id, started_at, evidence FROM trips.drive_spans
            WHERE person = %s AND source = %s AND ended_at IS NULL AND started_at < %s
            """,
            (person, SOURCE_PHONE, before),
        )
        closed = 0
        for span_id, started, ev in cur.fetchall():
            if skip and span_id in skip:
                continue
            ev = ev or {}
            last_seen = _ts(ev.get("last_seen_at"))
            if seen_before is not None and (last_seen is None or last_seen >= seen_before):
                continue
            last_signal = _ts(ev.get("last_signal_at")) or last_seen or started
            cur.execute(
                "UPDATE trips.drive_spans SET ended_at = %s, updated_at = now() WHERE id = %s",
                (max(started, last_signal), span_id),
            )
            closed += 1
        return closed

    def spans_needing_positions(self, limit: int = 50) -> list[Span]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, person, vehicle_id, started_at, ended_at, evidence, source FROM trips.drive_spans
                WHERE ended_at IS NOT NULL
                  AND (positions_fetched_at IS NULL OR positions_fetched_at < updated_at)
                ORDER BY ended_at LIMIT %s
                """,
                (limit,),
            )
            return [Span(*r[:5], evidence=r[5] or {}, source=r[6]) for r in cur.fetchall()]

    def insert_positions(self, points: Iterable[Position]) -> int:
        inserted = 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            for p in points:
                cur.execute(
                    """
                    INSERT INTO trips.trip_positions (person, ts, lat, lon, speed_kmh)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (person, ts) DO NOTHING
                    """,
                    (p.person, p.ts, p.lat, p.lon, p.speed_kmh),
                )
                inserted += max(cur.rowcount, 0)
        return inserted

    def mark_positions_fetched(self, span_id: int) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE trips.drive_spans SET positions_fetched_at = now() WHERE id = %s", (span_id,))

    # --------------------------------------------------------- projection

    def load_projection_inputs(
        self, window_start: datetime, now: datetime, existing_since: datetime | None = None
    ) -> tuple[list[Span], list[Reading], list[Position], dict[str, Trip]]:
        """``existing`` holds stored trips starting at/after
        min(window_start, existing_since) plus every imported trip
        (projection.project's contract)."""
        since = window_start - LOAD_MARGIN
        existing_from = min(window_start, existing_since) if existing_since else window_start
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, person, vehicle_id, started_at, ended_at, evidence, source FROM trips.drive_spans
                WHERE COALESCE(ended_at, %s) >= %s
                """,
                (now, since),
            )
            spans = [Span(*r[:5], evidence=r[5] or {}, source=r[6]) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT id, vehicle_id, km, data_captured_at FROM trips.odometer_readings
                WHERE data_captured_at >= %s
                UNION ALL
                SELECT * FROM (
                    SELECT DISTINCT ON (vehicle_id) id, vehicle_id, km, data_captured_at
                    FROM trips.odometer_readings WHERE data_captured_at < %s
                    ORDER BY vehicle_id, data_captured_at DESC, km DESC
                ) before_window
                """,
                (since, since),
            )
            readings = [Reading(*r) for r in cur.fetchall()]
            cur.execute(
                "SELECT person, ts, lat, lon, speed_kmh FROM trips.trip_positions WHERE ts >= %s",
                (since,),
            )
            positions = [Position(r[0], r[1], r[2], r[3], float(r[4]) if r[4] is not None else None) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT trip_key, vehicle_id, driver, started_at, ended_at, odo_start, odo_end, km, gps_km,
                       status, source, evidence
                FROM trips.trips WHERE started_at >= %s OR source = %s
                """,
                (existing_from, SOURCE_VW),
            )
            existing = {r[0]: Trip(*r[:11], evidence=r[11] or {}) for r in cur.fetchall()}
        return spans, readings, positions, existing

    def apply_projection(self, result: ProjectionResult) -> None:
        if not result.upserts and not result.removals and not result.log:
            return
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            for t in result.upserts:
                cur.execute(
                    """
                    INSERT INTO trips.trips (trip_key, vehicle_id, driver, started_at, ended_at, odo_start,
                                             odo_end, km, gps_km, status, source, evidence, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (trip_key) DO UPDATE SET
                        vehicle_id = EXCLUDED.vehicle_id, driver = EXCLUDED.driver,
                        started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at,
                        odo_start = EXCLUDED.odo_start, odo_end = EXCLUDED.odo_end, km = EXCLUDED.km,
                        gps_km = EXCLUDED.gps_km, status = EXCLUDED.status, source = EXCLUDED.source,
                        evidence = EXCLUDED.evidence, updated_at = now()
                    """,
                    (
                        t.trip_key, t.vehicle_id, t.driver, t.started_at, t.ended_at, t.odo_start, t.odo_end,
                        t.km, t.gps_km, t.status, t.source, Jsonb(t.evidence),
                    ),
                )
            for key in result.removals:
                cur.execute("DELETE FROM trips.trips WHERE trip_key = %s", (key,))
            for entry in result.log:
                cur.execute(
                    "INSERT INTO trips.trip_projection_log (trip_key, old, new, reason) VALUES (%s, %s, %s, %s)",
                    (
                        entry.trip_key,
                        Jsonb(entry.old) if entry.old is not None else None,
                        Jsonb(entry.new) if entry.new is not None else None,
                        entry.reason,
                    ),
                )


    # ------------------------------------------------------------ imports

    def vehicle_for_vin(self, vin: str) -> str | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM vehicle_pipeline.vehicles WHERE vin = %s", (vin,))
            row = cur.fetchone()
            return row[0] if row else None

    def known_row_hashes(self, hashes: Sequence[str]) -> set[str]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT row_hash FROM trips.imported_trips WHERE row_hash = ANY(%s)", (list(hashes),))
            return {r[0] for r in cur.fetchall()}

    def record_import(self, vehicle_id: str, export: ParsedExport, source: str = SOURCE_VW) -> dict[str, int]:
        """Idempotent, in one transaction: the export row, every exported
        row (new ones inserted, known ones -- same row_hash -- kept), and
        the export membership of all of them. Returns counts."""
        h = export.header
        stats = {"rows": len(export.rows), "inserted": 0, "already_known": 0, "export_new": 0}
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trips.import_exports (source, vehicle_id, export_created_at, export_odometer_km, engine)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, vehicle_id, export_created_at) DO NOTHING
                RETURNING id
                """,
                (source, vehicle_id, h.created_at, h.odometer_km, h.engine),
            )
            row = cur.fetchone()
            if row is not None:
                stats["export_new"] = 1
                export_id = row[0]
            else:
                cur.execute(
                    "SELECT id FROM trips.import_exports WHERE source = %s AND vehicle_id = %s AND export_created_at = %s",
                    (source, vehicle_id, h.created_at),
                )
                export_id = cur.fetchone()[0]  # type: ignore[index]
            for r in export.rows:
                rh = row_hash(vehicle_id, r, source)
                cur.execute(
                    """
                    INSERT INTO trips.imported_trips
                        (source, vehicle_id, kind, ended_at, km, driving_time, avg_speed_kmh, avg_consumption,
                         consumption_unit, total_consumption, total_consumption_unit, raw, export_created_at,
                         export_odometer_km, row_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (row_hash) DO NOTHING
                    RETURNING id
                    """,
                    (
                        source, vehicle_id, r.kind, r.ended_at, r.km, r.driving_time, r.avg_speed_kmh,
                        r.avg_consumption, r.consumption_unit, r.total_consumption, r.total_consumption_unit,
                        Jsonb(r.raw), h.created_at, h.odometer_km, rh,
                    ),
                )
                got = cur.fetchone()
                if got is not None:
                    stats["inserted"] += 1
                    trip_id = got[0]
                else:
                    stats["already_known"] += 1
                    cur.execute("SELECT id FROM trips.imported_trips WHERE row_hash = %s", (rh,))
                    trip_id = cur.fetchone()[0]  # type: ignore[index]
                cur.execute(
                    "INSERT INTO trips.imported_trip_exports (imported_trip_id, export_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (trip_id, export_id),
                )
        return stats

    def _import_rows(self, cur: Any, kind: str, vehicle_id: str | None = None) -> list[ImportedRow]:
        cur.execute(
            """
            SELECT t.id, t.source, t.vehicle_id, t.kind, t.ended_at, t.km, t.driving_time,
                   array_agg(m.export_id ORDER BY m.export_id)
            FROM trips.imported_trips t
            JOIN trips.imported_trip_exports m ON m.imported_trip_id = t.id
            WHERE t.kind = %s AND (%s::text IS NULL OR t.vehicle_id = %s)
            GROUP BY t.id
            ORDER BY t.vehicle_id, t.ended_at, t.id
            """,
            (kind, vehicle_id, vehicle_id),
        )
        return [ImportedRow(*r[:7], export_ids=tuple(r[7])) for r in cur.fetchall()]

    def _import_exports(self, cur: Any, vehicle_id: str | None = None) -> list[ImportExport]:
        cur.execute(
            "SELECT id, source, vehicle_id, export_created_at, export_odometer_km FROM trips.import_exports "
            "WHERE %s::text IS NULL OR vehicle_id = %s ORDER BY id",
            (vehicle_id, vehicle_id),
        )
        return [ImportExport(*r) for r in cur.fetchall()]

    def load_import_inputs(self) -> ImportInputs:
        """Everything the import projection needs, regardless of age: all
        exports, all short-term rows, and per export the first odometer
        reading at/after its last trip end (+ whether a detector span of
        the vehicle started in between)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            exports = self._import_exports(cur)
            if not exports:
                return ImportInputs()
            rows = self._import_rows(cur, KIND_SHORT_TERM)
            checks: dict[int, AnchorCheck] = {}
            for eid, (vehicle, last_end) in anchor_points(exports, rows).items():
                cur.execute(
                    """
                    SELECT id, vehicle_id, km, data_captured_at FROM trips.odometer_readings
                    WHERE vehicle_id = %s AND data_captured_at >= %s
                    ORDER BY data_captured_at, km DESC LIMIT 1
                    """,
                    (vehicle, last_end),
                )
                r = cur.fetchone()
                reading = Reading(*r) if r else None
                between = False
                if reading is not None:
                    cur.execute(
                        """
                        SELECT EXISTS (SELECT 1 FROM trips.drive_spans
                                       WHERE vehicle_id = %s AND started_at > %s AND started_at < %s)
                        """,
                        (vehicle, last_end + END_SLACK, reading.captured_at),
                    )
                    between = bool(cur.fetchone()[0])  # type: ignore[index]
                checks[eid] = AnchorCheck(reading, between)
        return ImportInputs(exports=exports, rows=rows, checks=checks)

    def refuel_check(self, vehicle_id: str) -> list[RefuelCheck]:
        """Odometer at each refuel of one vehicle (imports.refuel_crosscheck)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            exports = self._import_exports(cur, vehicle_id)
            rows = self._import_rows(cur, KIND_REFUEL, vehicle_id)
            cur.execute(
                """
                SELECT trip_key, vehicle_id, driver, started_at, ended_at, odo_start, odo_end, km, gps_km,
                       status, source, evidence
                FROM trips.trips WHERE vehicle_id = %s AND source = %s
                """,
                (vehicle_id, SOURCE_VW),
            )
            trips = [Trip(*r[:11], evidence=r[11] or {}) for r in cur.fetchall()]
        return refuel_crosscheck(exports, rows, trips)

    # -------------------------------------------------------- annotations

    def annotate(self, trip_keys: str | Sequence[str], *, via: str, actor: str | None = None, **values: Any) -> int:
        """The single write path for trip annotations (business/private
        flag, purpose, driver/vehicle override), used by the notification
        handler and the UI alike. Appends one row per trip and field in one
        transaction; the latest row per (trip, field) wins
        (trips.trips_effective). ``annotate(key, business=True,
        via="notify", actor="ha")``. Returns the number of rows written."""
        keys = [trip_keys] if isinstance(trip_keys, str) else list(dict.fromkeys(trip_keys))
        if via not in ANNOTATION_VIAS:
            raise AnnotationError(f"unknown via {via!r}")
        if not values:
            raise AnnotationError("nothing to annotate")
        for field, value in values.items():
            if isinstance(value, str):
                values[field] = value.strip() or None
            validate_annotation(field, values[field])
        if not keys:
            return 0
        written = 0
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            for key in keys:
                for field, value in values.items():
                    cur.execute(
                        "INSERT INTO trips.trip_annotations (trip_key, field, value, actor, via) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (key, field, Jsonb(value), actor, via),
                    )
                    written += 1
        return written

    def list_effective(
        self,
        *,
        vehicle: str | None = None,
        month_start: datetime | None = None,
        month_end: datetime | None = None,
        unflagged_only: bool = False,
        status: str | None = None,
        limit: int = 500,
    ) -> list[EffectiveTrip]:
        where = ["TRUE"]
        params: list[Any] = []
        if vehicle:
            where.append("vehicle_id = %s")
            params.append(vehicle)
        if month_start is not None:
            where.append("started_at >= %s")
            params.append(month_start)
        if month_end is not None:
            where.append("started_at < %s")
            params.append(month_end)
        if unflagged_only:
            where.append("business IS NULL")
        if status:
            where.append("status = %s")
            params.append(status)
        params.append(limit)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_EFFECTIVE_COLS} FROM trips.trips_effective WHERE {' AND '.join(where)} "
                "ORDER BY started_at DESC, trip_key LIMIT %s",
                params,
            )
            return [EffectiveTrip(*r) for r in cur.fetchall()]

    def get_effective(self, trip_key: str) -> EffectiveTrip | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_EFFECTIVE_COLS} FROM trips.trips_effective WHERE trip_key = %s", (trip_key,))
            row = cur.fetchone()
            return EffectiveTrip(*row) if row else None

    def vehicle_ids(self) -> list[str]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT vehicle_id FROM trips.trips_effective WHERE vehicle_id IS NOT NULL ORDER BY 1")
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------ notifications

    def ensure_notify_cutoff(self, now: datetime) -> datetime:
        """The persisted notification cutoff, written as ``now`` on the first
        call ever (first notify-enabled start) and never moved after."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trips.notify_state (id, cutoff) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (now,)
            )
            cur.execute("SELECT cutoff FROM trips.notify_state WHERE id = 1")
            return cur.fetchone()[0]  # type: ignore[index]

    def notify_candidates(self, cutoff: datetime, limit: int = 200) -> list[EffectiveTrip]:
        """Closed trips that ended after the cutoff and were never notified.
        The final eligibility filter (age, flag, rate limit) is
        trips.notify.eligible."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_EFFECTIVE_COLS} FROM trips.trips_effective e
                WHERE e.ended_at IS NOT NULL AND e.ended_at > %s
                  -- imported trips are history, never notified
                  AND e.source <> 'vw_export'
                  AND NOT EXISTS (SELECT 1 FROM trips.notifications_sent n WHERE n.trip_key = e.trip_key)
                ORDER BY e.ended_at, e.trip_key LIMIT %s
                """,
                (cutoff, limit),
            )
            return [EffectiveTrip(*r) for r in cur.fetchall()]

    def mark_notified(self, trip_key: str, notify_id: str, target: str) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trips.notifications_sent (trip_key, notify_id, target) VALUES (%s, %s, %s)
                ON CONFLICT (trip_key) DO UPDATE SET notify_id = EXCLUDED.notify_id, target = EXCLUDED.target
                """,
                (trip_key, notify_id, target),
            )

    def trip_key_for_notify_id(self, notify_id: str) -> str | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT trip_key FROM trips.notifications_sent WHERE notify_id = %s", (notify_id,))
            row = cur.fetchone()
            return row[0] if row else None


def merge_span_evidence(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Combine evidence of overlapping detections of one span. Windows
    overlap, so per-kind seconds and scores take the max rather than the
    sum."""

    def maxmap(a: dict | None, b: dict | None) -> dict:
        out = dict(a or {})
        for k, v in (b or {}).items():
            out[k] = max(v, out.get(k, v))
        return out

    return {
        **old,
        **new,
        "signals": sorted(set(old.get("signals") or []) | set(new.get("signals") or [])),
        "seconds": maxmap(old.get("seconds"), new.get("seconds")),
        "ssid_vehicles": maxmap(old.get("ssid_vehicles"), new.get("ssid_vehicles")),
        "scores": maxmap(old.get("scores"), new.get("scores")),
        "base_score": max(int(old.get("base_score") or 0), int(new.get("base_score") or 0)),
        "vehicle_via": old.get("vehicle_via") or new.get("vehicle_via"),
        "last_signal_at": max(
            (t for t in (_ts(old.get("last_signal_at")), _ts(new.get("last_signal_at"))) if t), default=None
        ).isoformat()
        if (old.get("last_signal_at") or new.get("last_signal_at"))
        else None,
    }


def _ts(value: Any) -> datetime | None:
    return parse_ts(value) if value else None
