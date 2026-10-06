"""Postgres access for the trip detector (``trips`` schema)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
        self, window_start: datetime, now: datetime
    ) -> tuple[list[Span], list[Reading], list[Position], dict[str, Trip]]:
        since = window_start - LOAD_MARGIN
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
                FROM trips.trips WHERE started_at >= %s
                """,
                (window_start,),
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
