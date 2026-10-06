"""Trip detector entrypoint: ``python -m vehicle_pipeline.trips.detector``.

Every TRIP_POLL_INTERVAL seconds (default 60):

1. odometer entities -> trips.odometer_readings (idempotent),
2. phone signal entities -> trips.drive_spans (per person),
3. tracker breadcrumbs for closed spans -> trips.trip_positions,
4. recompute the trips projection over the trailing TRIP_RECOMPUTE_DAYS,
   plus all imported in-car trip memories (trips.import_vw) in full,
5. with TRIP_NOTIFY_ENABLED: send business/private notifications for newly
   closed trips (trips.notify); answers arrive on a websocket thread.

Each entity has a cursor (trips.cursors); a cycle fetches
[cursor - CURSOR_OVERLAP, now]. With no cursor yet it backfills
TRIP_BACKFILL_DAYS in one-day chunks. A heartbeat file feeds the
liveness probe.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from vehicle_pipeline.trips.ha import HAClient, HistorySource
from vehicle_pipeline.trips.imports import earliest_start
from vehicle_pipeline.trips.notify import ActionListener, Notifier
from vehicle_pipeline.trips.projection import project
from vehicle_pipeline.trips.schema import init_trips_schema
from vehicle_pipeline.trips.settings import TripSettings
from vehicle_pipeline.trips.signals import (
    breadcrumbs_from_tracker,
    detect_spans,
    extract_readings,
)
from vehicle_pipeline.trips.store import TripStore
from vehicle_pipeline.trips.util import touch_heartbeat
from vehicle_pipeline.vollkosten import report_tz

logger = logging.getLogger("vehicle_pipeline.trips.detector")

CURSOR_OVERLAP = timedelta(minutes=10)
CHUNK = timedelta(days=1)
POSITION_PAD = timedelta(minutes=1)


def windows(cursor: datetime | None, now: datetime, backfill_days: int) -> Iterator[tuple[datetime, datetime]]:
    start = cursor - CURSOR_OVERLAP if cursor is not None else now - timedelta(days=backfill_days)
    while True:
        end = min(start + CHUNK, now)
        yield start, end
        if end >= now:
            return
        start = end - CURSOR_OVERLAP


def run_cycle(
    settings: TripSettings,
    ha: HistorySource,
    store: TripStore,
    now: datetime,
    beat: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {"readings_new": 0, "spans": 0, "positions_new": 0}

    for v in settings.vehicles:
        if not v.odometer_entity:
            continue
        for ws, we in windows(store.get_cursor(v.odometer_entity), now, settings.backfill_days):
            history = ha.fetch_history([v.odometer_entity], ws, we, significant_changes_only=False)
            stats["readings_new"] += store.insert_readings(extract_readings(v.slug, history.get(v.odometer_entity, [])))
            store.set_cursor(v.odometer_entity, we)
            beat()

    for phone in settings.phones:
        entities = phone.signal_entities()
        if not entities:
            continue
        cursors = [store.get_cursor(e) for e in entities]
        cursor = None if any(c is None for c in cursors) else min(c for c in cursors if c is not None)
        for ws, we in windows(cursor, now, settings.backfill_days):
            history = ha.fetch_history(entities, ws, we)
            detected = detect_spans(phone, history, settings.vehicles, we)
            store.record_spans(phone.person, detected, ws, we)
            stats["spans"] += len(detected)
            for e in entities:
                store.set_cursor(e, we)
            beat()

    trackers = {p.person: p.tracker for p in settings.phones if p.tracker}
    for span in store.spans_needing_positions():
        tracker = trackers.get(span.person)
        if tracker and span.ended_at is not None:
            history = ha.fetch_history([tracker], span.started_at - POSITION_PAD, span.ended_at + POSITION_PAD)
            stats["positions_new"] += store.insert_positions(breadcrumbs_from_tracker(span.person, history.get(tracker, [])))
        store.mark_positions_fetched(span.id)
        beat()

    window_start = now - timedelta(days=settings.recompute_days)
    # Imported trip memories (VW export) are recomputed in full every cycle.
    imports = store.load_import_inputs()
    spans, readings, positions, existing = store.load_projection_inputs(
        window_start, now, existing_since=earliest_start(imports)
    )
    result = project(
        vehicles=[v.slug for v in settings.vehicles],
        spans=spans,
        readings=readings,
        positions=positions,
        existing=existing,
        now=now,
        window_start=window_start,
        imports=imports,
    )
    store.apply_projection(result)
    stats.update(
        trips=len(result.trips),
        upserts=len(result.upserts),
        removals=len(result.removals),
        log_rows=len(result.log),
        **result.stats,
    )
    return stats


def build_notifier(settings: TripSettings, store: TripStore, ha: HAClient) -> Notifier | None:
    if not settings.notify_enabled:
        logger.info("trip notifications disabled (TRIP_NOTIFY_ENABLED unset/false)")
        return None
    targets = settings.notify_targets()
    if not targets:
        logger.warning(
            "TRIP_NOTIFY_ENABLED is set but no person in TRIP_PHONES has \"notify\": true; notifications disabled"
        )
        return None
    return Notifier(
        store=store,
        ha=ha,
        targets=targets,
        vehicle_names={v.slug: v.name for v in settings.vehicles},
        tz=ZoneInfo(report_tz()),
        since=settings.notify_since,
        max_per_cycle=settings.notify_max_per_cycle,
        min_age=timedelta(minutes=settings.notify_min_age_minutes),
        public_base_url=settings.public_base_url,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = TripSettings.from_env()
    logger.info(
        "trip detector starting: %d vehicle(s) (%d with odometer), %d person(s), interval %ss",
        len(settings.vehicles),
        sum(1 for v in settings.vehicles if v.odometer_entity),
        len(settings.phones),
        settings.poll_interval_seconds,
    )
    stop = {"flag": False}

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("signal %s received, stopping after this cycle", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def beat() -> None:
        touch_heartbeat(settings.heartbeat_file)

    beat()
    pool = ConnectionPool(settings.database_url, min_size=1, max_size=3, open=True)
    ha = HAClient(settings.ha_url, settings.ha_token)
    listener: ActionListener | None = None
    try:
        init_trips_schema(pool)
        store = TripStore(pool)
        notifier = build_notifier(settings, store, ha)
        if notifier is not None:
            notifier.start(datetime.now(UTC))
            listener = ActionListener(settings.ha_url, settings.ha_token, store)
            listener.start()
        while not stop["flag"]:
            beat()
            started = time.monotonic()
            try:
                stats = run_cycle(settings, ha, store, datetime.now(UTC), beat)
                logger.info("cycle ok in %.1fs: %s", time.monotonic() - started, stats)
            except Exception:
                logger.exception("cycle failed")
            beat()
            if notifier is not None:
                try:
                    nstats = notifier.run_once(datetime.now(UTC))
                    if any(nstats.values()):
                        logger.info("notifications: %s", nstats)
                except Exception:
                    logger.exception("notification pass failed")
                beat()
            deadline = time.monotonic() + settings.poll_interval_seconds
            while not stop["flag"] and time.monotonic() < deadline:
                time.sleep(1)
    finally:
        if listener is not None:
            listener.stop()
        ha.close()
        pool.close()


if __name__ == "__main__":
    main()
