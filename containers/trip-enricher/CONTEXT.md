# trip-enricher

Enriches MyGarage's WiCAN-detected drive sessions with driver attribution, GPS, and tax-relevant business/private classification.

## Language

**Attributed driver**:
The person (Jonas or Julia) a trip session is assigned to, inferred from Home Assistant phone signals (hotspot SSID, CarPlay, Automotive activity). Distinct from who is asked to classify a trip — only Jonas is ever asked, regardless of attributed driver.

**Business trip** / **Private trip**:
The tax-relevant classification of a trip, stored in `trip_records.business`. A trip is Private unless Jonas has actively reclassified it Business. Julia-attributed trips are always Private and are never surfaced for classification.

**Pending review**:
A trip whose classification has not yet been confirmed or overridden by Jonas. Pending trips are never silently dropped — they resurface in every subsequent weekly digest until acted on.

**Learned pattern**:
A route/time-of-day shape that has received 3 consistent manual classifications from Jonas. Once learned, new trips matching the pattern are auto-classified accordingly without appearing in the weekly review — but remain overridable per-trip if a normally-routine route was an exception that day.
_Avoid_: rule, heuristic (both used loosely elsewhere; "learned pattern" is the precise term for this specific auto-classify mechanism).
