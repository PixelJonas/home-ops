# Per-trip business/private notification for every trip, odometer from the EU Data Act feed, trips as a projection

**Status**: accepted (2026-10-06) — supersedes [ADR 0001](0001-weekly-review-trip-classification.md)

The Vollkostenrechnung moved from trip-enricher/MyGarage into `vehicle-pipeline` (`containers/vehicle-pipeline`, package `vehicle_pipeline.trips`). Its trip data and classification flow differ from what ADR 0001 decided, by the owner's decision of 2026-10-06.

## Decision

- **Every trip of every vehicle triggers one notification**, whoever drove (including trips attributed to other household members and odometer-gap trips with no known driver). ADR 0001's "other household members' trips are always Private and never asked about" no longer holds.
- **Only one recipient is asked**: the person whose business kilometres are claimed (ADR 0001's "only person ever asked"). Configured in the existing phone-config secret (`"notify": true` on that person's entry, optional `"notify_service"`), never in git.
- The notification is an HA actionable notification with two actions (Business / Private), sent shortly after the trip closes (≥ 10 min, rate-limited per cycle); the answer is written as an append-only annotation (`trips.trip_annotations`, `via = notify`). The trips page in the vehicle-pipeline UI is the fallback and bulk tool (`via = ui`). Both go through one write path (`TripStore.annotate`), keeping ADR 0001's single-write-path principle.
- Unanswered trips stay **unflagged** (`business IS NULL`), not silently Private: the Vollkostenrechnung views count them and mark the period provisional until every trip is flagged. No weekly digest and no pattern auto-classification for now.
- **Kilometres come from the odometer** of the VW EU Data Act feed (Home Assistant sensors), not from WiCAN sessions or GPS; GPS distance is only a fallback/plausibility value while the odometer reading is pending.
- **Trips are a projection**: the raw capture (odometer readings, phone drive spans, GPS breadcrumbs, annotations) is the record; `trips.trips` is recomputed from it and may be rebuilt at any time. Annotations are keyed by the stable trip key and applied on the read side (`trips.trips_effective`).
- Trips that ended before notifications were first enabled (the backfill) are never notified; they are flagged on the trips page.

## Considered Options

- **Keep the weekly digest (ADR 0001)** — rejected: a per-trip prompt right after parking is answered while the trip is still remembered; the trips page covers missed or batch cases.
- **Ask each driver about their own trips** — rejected: only one person's business kilometres matter for the filing, and that person knows (or decides) the purpose of trips in the shared cars.
- **Default unanswered trips to Private** — rejected for the computation: an unanswered trip is shown as open so the business share is never silently understated or overstated.

## Consequences

- One notification per trip can be noisy on busy days; the per-cycle cap and the trip-closing delay bound it, the tag replaces duplicates.
- Taps while the detector is down are lost (the answer listener is a websocket held by the detector); the trip stays open on the trips page.
- A trip key can change when the projection merges spans differently; the new key is notified again and the old annotation stays on the record.
