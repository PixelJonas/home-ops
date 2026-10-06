"""Vollkostenrechnung views: per vehicle and month / year, the full cost
(vehicle_pipeline.costs) against the kilometres driven (trips.trips_effective)
and the business share of it.

The views live in the ``vehicle_pipeline`` schema, not in ``trips``: they
are the cost-accounting output and join the cost ledger (owned by this
schema, like the vehicles table) with the trip read side. ``trips`` stays
the raw capture plus a projection that may be truncated and rebuilt at any
time; nothing there depends on costs.

Columns (both views):

* ``cost_total`` -- sum of ``costs.amount_gross``, financing included;
  ``cost_financing`` separately and ``cost_excl_financing`` = the
  difference (loan/leasing principal is arguably not a running cost, so
  both totals are exposed); one ``cost_<category>`` column per category.
* ``km`` -- odometer kilometres of trips whose odometer is known (status
  ``ok`` / ``odometer_split``). Trips still ``odometer_pending`` (the feed
  lags) or ``odometer_stale`` are NOT in ``km``; their GPS distance is in
  ``km_gps_unconfirmed`` and they are counted in ``trips_km_unknown``.
* ``km_business`` -- odometer km of trips flagged business;
  ``km_unflagged`` -- odometer km of trips not flagged either way.
* ``cost_per_km`` = cost_total / km; ``business_cost`` = cost_per_km x
  km_business (and the ``_excl_financing`` variants); ``business_share`` =
  km_business / km.
* ``complete`` -- true only when no trip in the period has an unknown
  odometer or a missing business/private flag. A ``business_cost`` with
  ``complete = false`` is provisional, never silently final.

Monthly cost per km is noisy (yearly insurance/tax bills land in one
month); the yearly view is the one to file.
"""

from __future__ import annotations

import os
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import sql

COST_CATEGORIES = (
    "fuel",
    "charging",
    "service",
    "insurance",
    "tax",
    "financing",
    "def",
    "parking",
    "toll",
    "other",
)
KM_KNOWN = "('ok', 'odometer_split')"
PERIODS = {"monthly": "month", "yearly": "year"}


def report_tz() -> str:
    """Timezone trips are bucketed into periods in: the container's TZ env
    (set in the manifest), else UTC."""
    tz = os.environ.get("TZ") or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return tz


def _view_sql(name: str, unit: str) -> str:
    cat_cols = ",\n        ".join(
        f"COALESCE(sum(amount_gross) FILTER (WHERE category = '{c}'), 0) AS cost_{c}" for c in COST_CATEGORIES
    )
    cat_out = ",\n    ".join(f"COALESCE(c.cost_{c}, 0) AS cost_{c}" for c in COST_CATEGORIES)
    return f"""
DROP VIEW IF EXISTS vehicle_pipeline.vollkosten_{name};
CREATE VIEW vehicle_pipeline.vollkosten_{name} AS
WITH c AS (
    SELECT
        vehicle_id,
        date_trunc('{unit}', date)::date AS period_start,
        sum(amount_gross) AS cost_total,
        count(*) AS cost_entries,
        {cat_cols}
    FROM vehicle_pipeline.costs
    GROUP BY 1, 2
),
t AS (
    SELECT
        vehicle_id,
        date_trunc('{unit}', started_at AT TIME ZONE {{tz}})::date AS period_start,
        count(*) AS trips_total,
        count(*) FILTER (WHERE status NOT IN {KM_KNOWN}) AS trips_km_unknown,
        count(*) FILTER (WHERE business IS NULL) AS trips_unflagged,
        count(*) FILTER (WHERE business) AS trips_business,
        COALESCE(sum(km) FILTER (WHERE status IN {KM_KNOWN}), 0) AS km,
        COALESCE(sum(gps_km) FILTER (WHERE status NOT IN {KM_KNOWN}), 0) AS km_gps_unconfirmed,
        COALESCE(sum(km) FILTER (WHERE business AND status IN {KM_KNOWN}), 0) AS km_business,
        COALESCE(sum(gps_km) FILTER (WHERE business AND status NOT IN {KM_KNOWN}), 0)
            AS km_business_gps_unconfirmed,
        COALESCE(sum(km) FILTER (WHERE business IS NULL AND status IN {KM_KNOWN}), 0) AS km_unflagged
    FROM trips.trips_effective
    WHERE vehicle_id IS NOT NULL
    GROUP BY 1, 2
),
j AS (
    SELECT
        COALESCE(c.vehicle_id, t.vehicle_id) AS vehicle_id,
        COALESCE(c.period_start, t.period_start) AS period_start,
        COALESCE(c.cost_total, 0) AS cost_total,
        COALESCE(c.cost_total, 0) - COALESCE(c.cost_financing, 0) AS cost_excl_financing,
        COALESCE(c.cost_entries, 0) AS cost_entries,
        {cat_out},
        COALESCE(t.trips_total, 0) AS trips_total,
        COALESCE(t.trips_km_unknown, 0) AS trips_km_unknown,
        COALESCE(t.trips_unflagged, 0) AS trips_unflagged,
        COALESCE(t.trips_business, 0) AS trips_business,
        COALESCE(t.km, 0) AS km,
        COALESCE(t.km_gps_unconfirmed, 0) AS km_gps_unconfirmed,
        COALESCE(t.km_business, 0) AS km_business,
        COALESCE(t.km_business_gps_unconfirmed, 0) AS km_business_gps_unconfirmed,
        COALESCE(t.km_unflagged, 0) AS km_unflagged
    FROM c
    FULL OUTER JOIN t ON t.vehicle_id = c.vehicle_id AND t.period_start = c.period_start
)
SELECT
    j.*,
    round(cost_total / NULLIF(km, 0), 4) AS cost_per_km,
    round(cost_excl_financing / NULLIF(km, 0), 4) AS cost_per_km_excl_financing,
    round(cost_total * km_business / NULLIF(km, 0), 2) AS business_cost,
    round(cost_excl_financing * km_business / NULLIF(km, 0), 2) AS business_cost_excl_financing,
    round(km_business / NULLIF(km, 0), 4) AS business_share,
    (trips_km_unknown = 0 AND trips_unflagged = 0) AS complete
FROM j;
"""


def vollkosten_ddl(tz: str | None = None) -> sql.Composed:
    """DDL for vehicle_pipeline.vollkosten_monthly / _yearly. The views are
    leaves (nothing depends on them), so they are dropped and recreated on
    every start; needs vehicle_pipeline.costs and trips.trips_effective."""
    tz = tz or report_tz()
    return sql.Composed(
        [sql.SQL(_view_sql(name, unit)).format(tz=sql.Literal(tz)) for name, unit in PERIODS.items()]
    )


def load_vollkosten(pool: Any, period: str = "yearly", vehicle: str | None = None) -> list[dict[str, Any]]:
    if period not in PERIODS:
        raise ValueError(f"unknown period {period!r}")
    query = sql.SQL(
        "SELECT * FROM vehicle_pipeline.{view} WHERE (%(v)s::text IS NULL OR vehicle_id = %(v)s) "
        "ORDER BY period_start DESC, vehicle_id"
    ).format(view=sql.Identifier(f"vollkosten_{period}"))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(query, {"v": vehicle})
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
