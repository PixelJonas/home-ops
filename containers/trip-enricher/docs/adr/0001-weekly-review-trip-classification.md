# Weekly digest + single write-path API for business/private trip classification

**Status**: accepted

trip-enricher needs to decide business/private for every trip it enriches — the data feeds Jonas's German tax filing, so getting the default and the review cadence wrong has real (if small) financial consequences either direction. Decided during wayfinder ticket infra-ops#9 (part of infra-ops#5).

## Decision

- Classification defaults to **Private** and only flips to Business when Jonas actively reclassifies it. Never the reverse.
- Review happens on a **weekly** digest, not per-trip and not purely on-demand: one Home Assistant actionable notification (reusing the existing `confirmable_notification` blueprint pattern already used elsewhere in this homelab) links to a review page listing that week's trips plus any still-pending backlog from prior weeks.
- Once a route/time-of-day pattern gets 3 consistent manual answers, it auto-classifies going forward without appearing in the review — always still overridable per-trip.
- All classification writes go through **one API endpoint** on trip-enricher (`PATCH`-style, one trip at a time), used by both the review page and, later, other callers (Claude Code sessions, Heimdall) — never a direct DB write from any caller.
- Julia-attributed trips are always Private and never enter the review flow; only Jonas is ever asked.

## Considered Options

- **Business-until-confirmed-private default** — rejected. Under-claiming a deduction is a minor annoyance caught on review; over-claiming a business trip that's never actually verified is the higher-risk failure mode. Default to the side that's safe if a trip is never reviewed at all.
- **Per-trip immediate prompt** — rejected. Risks pinging Jonas mid-drive or right after getting home; a weekly batch is lower-friction and matches how the data is actually consumed (periodic tax bookkeeping, not real-time).
- **Direct SQL/DB writes from the review page or HA automation** — rejected. Duplicates trip-enricher's own data-access and validation logic (including the pattern-learning rule) across every caller. A single API endpoint keeps that logic in one place as more callers (agents, not just the review page) show up.
- **Cross-referencing Jonas's employer (Red Hat) expense system (Spesenabrechnung) as a signal** — rejected for now. It's copy-paste-only with no accessible export/API, so the mechanism relies purely on GPS/driver/time signals it already has. Revisit only if that system ever becomes programmatically reachable.
- **Silently dropping unreviewed trips at the default** — rejected. They resurface in every subsequent week's digest instead, so a missed business trip stays visible as a "pending" item rather than quietly aging into a wrong default forever.

## Consequences

- The review page's exact hosting (trip-enricher itself, MyGarage, or an HA dashboard) is a build-time choice, not fixed by this ADR.
- Historical trip data (pre-dating this mechanism, sourced from the Vollkostenrechnung spreadsheet's Kilometerstand sheet) is out of scope here — see infra-ops#10 — and needs to land through a compatible path, not a separate one-off import mechanism, once designed.
