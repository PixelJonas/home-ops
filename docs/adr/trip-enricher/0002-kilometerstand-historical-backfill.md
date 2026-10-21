# Represent Kilometerstand periods as paired business/private trip_records rows

**Status**: accepted

Kilometerstand (the Vollkostenrechnung spreadsheet's pre-WiCAN odometer log for the Multivan) is Jonas's real historical business-trip record: 19 periods between odometer readings, each with a business-km subset and a free-text note. Decided during wayfinder ticket infra-ops#10, part of infra-ops#5. `trip_records.business` is a boolean, so a period's mixed business/private split can't fit in one row.

## Decision

- Each period backfills as **up to two synthetic `trip_records` rows**, sharing the period's real `odometer_start`/`odometer_end` and raw note verbatim: one `business=true` row for the business-km slice, one `business=false` row for the remainder. A side is omitted when its distance would be 0 — a zero-distance row is noise, not data.
- `driver` is hardcoded `"Jonas"` — Kilometerstand exists specifically to track his employer-relevant business km.
- `business` is written directly, never `NULL`. Backfilled rows never enter the weekly review — they're Jonas's own already-finalized numbers, not something to re-litigate.
- `evidence` carries `{"source": "kilometerstand-backfill", ...}` so these rows stay identifiable against live WiCAN sessions.
- `session_id` is deterministic: `kilometerstand-<period-end-date>-business` / `-private`.
- The backfill writes via a **one-off script doing direct SQL** (`INSERT ... ON CONFLICT (session_id) DO NOTHING`, `distance_km_effective = distance_km`), bypassing `insert_trip()`/`compute_effective_distance()`. That helper's odometer-overlap dedup exists to catch WiCAN phantom-session double-counting; it would incorrectly zero out one of the two synthetic rows here, since they deliberately share an odometer range.
- No leg-splitting for multi-stop periods (e.g. "HOME → Kleinheubach → Düsseldorf → Hamburg → Home") — Kilometerstand has no sub-period distance data to split by, so inventing one would fabricate precision that was never recorded.

## Considered Options

- **One row per period with a new numeric column** (e.g. `business_km`) instead of the boolean split — rejected: needs a schema change, and downstream business-km queries would have to special-case granularity between historical and live rows.
- **Separate table for period-level historical data** — rejected: same special-casing problem, plus duplicates `trip_records`' shape for no real benefit, since the two-row model fits the existing schema exactly.

## Consequences

- Confirmed via a live DB check (2026-09-10): `trips.trip_records` has zero rows for the Multivan's VIN at all — this is a pure backfill, not a merge against existing sessions.
- The actual backfill script and its 19-period source data aren't written yet — this ADR is a decision record for a later implementation session, per infra-ops#5's map being decision-only.
