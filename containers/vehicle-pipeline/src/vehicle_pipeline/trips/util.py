"""Small helpers ported from containers/trip-enricher/app/trip_enricher.py
(with_retry, touch_heartbeat, parse_ts, haversine_km). Kept behaviourally
identical apart from using the logging module and typed signatures."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any, TypeVar

logger = logging.getLogger("vehicle_pipeline.trips")

T = TypeVar("T")


def touch_heartbeat(path: str) -> None:
    """Ported from trip_enricher.touch_heartbeat: the liveness probe checks
    this file's mtime."""
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        logger.warning("heartbeat write failed: %s", e)


def with_retry(
    fn: Callable[[], T],
    desc: str,
    attempts: int = 4,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Ported from trip_enricher.with_retry: exponential backoff, re-raises
    the last error."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - retry anything, like the original
            last = e
            delay = base_delay * (2**i)
            logger.warning("retrying %s after error (attempt %d/%d, %.0fs): %s", desc, i + 1, attempts, delay, e)
            if i + 1 < attempts:
                sleep(delay)
    assert last is not None
    raise last


def parse_ts(value: Any) -> datetime | None:
    """Ported from trip_enricher.parse_ts, including its naive->UTC fix: a
    naive ISO string is assumed to be UTC rather than local time."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        if value > 1e12:  # epoch milliseconds
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_ts(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("unparseable timestamp: %r", value)
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def haversine_km(points: Sequence[Any]) -> float:
    """Ported from trip_enricher.haversine_km: sum of great-circle distances
    between consecutive points (dicts or objects with lat/lon)."""

    def ll(p: Any) -> tuple[float, float]:
        return (p["lat"], p["lon"]) if isinstance(p, dict) else (p.lat, p.lon)

    total = 0.0
    for a, b in zip(points, points[1:], strict=False):
        alat, alon = ll(a)
        blat, blon = ll(b)
        dlat = radians(blat - alat)
        dlon = radians(blon - alon)
        h = sin(dlat / 2) ** 2 + cos(radians(alat)) * cos(radians(blat)) * sin(dlon / 2) ** 2
        total += 6371.0 * 2 * asin(sqrt(h))
    return total
