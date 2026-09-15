# Vehicle Cost Pipeline (map #5) — Continuation & Open Tasks

**Status as of 2026-09-15.** This document is the handover point for whoever
(human or agent) picks this work up next. It assumes no memory of the
session that produced it — everything needed to continue is here or linked.

**Source of truth for design decisions:** Gitea `git.janz.digital/jonas/infra-ops`,
wayfinder map #5 ("Vehicle tax-cost picture: MyGarage + trip-enricher") and its
child tickets (#16–#19, #38, #44). All design questions on the *finished*
pieces below are already answered there — don't re-litigate them. The
original implementation plan for #16 is at
`docs/superpowers/plans/2026-09-11-vehicle-cost-pipeline.md` in this repo
(home-ops) — read that first for the pipeline's architecture; this document
only covers what changed or was learned *after* that plan was written.

## TL;DR — what actually works right now

The `vehicle-pipeline` service (`containers/vehicle-pipeline/`,
`components-apps/vehicle-pipeline/`) is deployed on Altus, running, and has
been proven end-to-end against real documents: Paperless webhook + poll
reconciliation → LiteLLM extraction → taxonomy mapping → human-reviewed
draft → (once approved) a real write to MyGarage's REST API. The review UI
is at `https://vehicle-pipeline.app.janz.digital/review`, gated by Authelia
(login there, not basic-auth — see "Auth architecture" below).

**#16 (the pipeline itself): done, deployed, verified working.**
**#19 (ID.4 historical backfill): done** — all 9 processable documents are
drafted in the review queue, `pending`, waiting for Jonas to approve/correct
in the UI.
**#17 (Multivan historical backfill): mostly done** — 12 of 14 known cost
entries are already written directly to MyGarage (see the exact list
below). Two items are intentionally blocked; two more need one small
external step to finish.
**#38, #9/#10: not started** (see "Not yet started" section).

## Auth architecture (don't rebuild what's already there)

`/review` is **not** on a public OpenShift Route. It's reachable only via a
`tailscale.com/expose` annotation on `vehicle-pipeline-app`'s Service
(`components-apps/vehicle-pipeline/vehicle-pipeline-app.yaml`), fronted by
Caddy on the Synology (`infra-ops` repo, `stacks/synology/caddy` +
`stacks/synology/authelia`) at `vehicle-pipeline.app.janz.digital`, gated by
Authelia (`INFRA_PROXY_HOSTS` Doppler key in `infra-ops/prd`, entry
`vehicle-pipeline`, `auth: true`). This required deploying the Tailscale K8s
operator to Altus for the first time (it previously only existed on Sakaar)
— see `components-infra/tailscale-operator/base` (reused unmodified) and its
registration in `bootstrap/overlays/local.home/values-infra.yaml`.

If `/review` ever stops being reachable: check (1) the `ts-vehicle-pipeline-app-*`
pod is running in the `tailscale` namespace on Altus, (2) its current
Tailscale IP still matches the `backend` field in infra-ops's
`INFRA_PROXY_HOSTS` `vehicle-pipeline` entry (the IP can change if the proxy
pod is recreated — there's no MagicDNS hostname wired in, just a raw IP,
matching `pricing-tool`'s own precedent on Sakaar), (3) `terraform apply` was
run on **both** `synology-caddy` and `synology-authelia` after any
`INFRA_PROXY_HOSTS` change (easy to forget the second one — that's exactly
what caused an "authenticated but still Forbidden" bug this session), and
(4) both containers were restarts after their Terraform apply (bind-mount
inode-pinning, same as the repo's other Caddy gotchas).

## Real bugs found and fixed this session (all merged to `main`, all deployed)

These were found by actually testing the pipeline against real documents and
a real MyGarage instance — not caught by any of the original unit tests,
since those all mocked the transport. If you're debugging something that
looks similar, check these are still fixed (they are, as of `main` at the
time of writing) before re-diagnosing from scratch:

1. **`MYGARAGE_VEHICLES`'s real shape** (PR #435) — it's a list of rich
   vehicle objects shared with WiCAN/trip-enricher, not `{vin: label}`.
2. **Wrong LiteLLM model + swallowed HTTP errors** (PR #436) — `gpt-4o-mini`
   was never registered on this deployment; also `LiteLLMExtractor.extract`
   didn't wrap `httpx.HTTPStatusError`, so any LLM-gateway failure crashed
   the whole reconciliation pass instead of degrading to a generic draft.
3. **`ersatzteile` routed to the cost-less `documents` fallback** (PR #437)
   — moved to `service-visits`, which actually has cost fields.
4. **`service-visits` drafts had empty `line_items`** (PR #438) — MyGarage's
   `tax-deduction-pdf` report reads *only* `line_items[].cost`, never
   `total_cost`. A correctly-costed record was still invisible to the one
   report this whole pipeline exists to feed.
5. **Every MyGarage write was rejected with a 403** (PR #447) — MyGarage
   requires an `X-CSRF-Token` header (returned as `csrf_token` alongside
   `access_token` at login); `MyGarageClient` never sent it. This affected
   *every* entity type — the review UI's approve button was broken for
   every pending draft until this shipped. Nobody had approved anything
   yet, so it was never caught in practice.
6. **`missed_fillup` is not a valid odometer bypass** (PR #448) — corrects
   an over-hasty first attempt at fixing "fuel records need `odometer_km`
   or a 422." Real fix: leave `odometer_km` null when unknown, let the
   human fill it in via the review-edit form (already generically
   supported — every payload key gets an editable input).
7. **Vehicle record had no `fuel_type` set** — not a code bug, a live data
   gap on the Multivan's MyGarage vehicle record (blocked DEF/AdBlue
   writes, which are diesel-only). Fixed live via `PUT /api/vehicles/{vin}`
   (not `PATCH` — that 405s) with `{"fuel_type": "Diesel"}`. Worth checking
   the ID.4's vehicle record has its own `fuel_type` set correctly too
   (it's a BEV, so DEF shouldn't apply, but check `fuel_economy_*` fields
   etc. are sane) before its first fuel-adjacent write.

## #19 — ID.4 historical backfill: done

All 9 processable documents (of the 10-document inventory from ticket #18)
were tagged `Fahrzeug:ID4-auto` (Paperless tag id **1750**) and processed by
the live pipeline into `pending` review-queue drafts. The 10th (#1787, the
Erstberechnung leasing invoice) was deliberately **not** tagged — see
"Blocked on the MyGarage fork" below.

| Paperless doc | MyGarage entity (draft) | Status |
|---|---|---|
| #2320 (tire repair) | service-visits | pending review |
| #2286 (tire storage) | service-visits | pending review |
| #2048 (insurance T&C update) | insurance | pending review |
| #1786 (initial premium invoice) | insurance | pending review |
| #1778 (insurance contract — LLM found a real €1030.25 premium here too; check this isn't a duplicate of #1786 before approving both) | insurance | pending review |
| #1776 (KFZ-Steuer) | tax-records | pending review — extracted €66.75 for the *post-exemption* period 2031-01-01–2031-11-24, not €0; the vehicle is BEV-exempt through 2030-12-31, both facts are true, just check which one you want on record |
| #2201 (Zulassung Teil I) | documents (not_cost_relevant) | pending review |
| #1745 (Abholschein) | documents (not_cost_relevant) | pending review |
| #1742 (precontract — LLM found real lease figures here matching ticket #44's €538.51/month, correctly still fell to `documents` since financing has no entity yet) | documents (finanzierung_zinsen) | pending review |
| #2310 (OOONO P-DISC, shared purchase with the Multivan) | service-visits, €34.95 | **already approved/written directly** (see below — this doc wasn't in #18's original list, found mid-session) |

**Next step:** none required from an implementation standpoint — this is
just waiting on Jonas to review/approve/correct each draft at `/review`.

## #17 — Multivan historical backfill: mostly done

Source data: `KFZ-Vollkosten.xlsx`, kept off-repo per infra-ops's public-safe
design (real personal financial/VIN/plate data) — see ticket #8's resolution
comment for the full sheet structure. Re-fetch via
`scp <vanaheim-user>@<vanaheim-host>:/Users/jjanz/Downloads/KFZ-Vollkosten.xlsx`
(vanaheim = Julia's MacBook; SSH host/user in Doppler
`VANAHEIM_SSH_HOST`/`VANAHEIM_SSH_USER`, `infra-ops/prd` — it's a personal
laptop, not always reachable, no fixed uptime guarantee). Delete the local
copy again once done reading it; never commit it.

These are **direct writes**, bypassing the LLM/review-queue entirely (ticket
#17's own decision — the spreadsheet's human-entered category/amount/date
are already trusted). Every doc below is tagged `Fahrzeug:Multivan-auto`
(id **1751**) + `MyGarage:imported` (id **1752**, created this session — a
generic, non-vehicle-scoped marker reused by #19 and any future backfill).

**Written and confirmed (12 of 14 known entries):**

| Doc | Category | Amount | MyGarage entity |
|---|---|---|---|
| #2289 | Kraftstoff | €95.70 | fuel (odometer 15,729 km) |
| #2293 | Kraftstoff | €94.88 | fuel (odometer 17,072 km) |
| #2294 | Kraftstoff | €100.87 | fuel (odometer 17,821 km) |
| #2274 | Kraftstoff | €123.65 | **not yet written** — see below |
| #2304 | Kraftstoff | €96.81 | fuel (odometer 19,447 km) |
| #2305 | Kraftstoff | €110.76 | fuel (odometer 20,855 km) |
| #2306 | Kraftstoff/AdBlue | €24.95 | def |
| #2307 | Autowäsche/Pflege | €16.99 | service-visits/Detailing |
| #2308 | Öl/Betriebsstoffe | €26.99 | service-visits/Maintenance |
| #2309 | Öl/Betriebsstoffe | €19.16 | service-visits/Maintenance |
| #2310 | Ersatzteile | €34.95 | service-visits/Maintenance (this doc covers 2 units, one per vehicle — see #19 table above for the ID.4's matching write) |

**Not yet written: #2274 (€123.65, Kraftstoff).** Its receipt has an internal
date contradiction (fiscal TSE timestamp vs. printed footer disagreed by 2
years) that was never resolved the way #2289/#2293/#2294 were — Jonas fixed
those three directly in Paperless, but #2274 wasn't confirmed fixed or given
an odometer reading before this session ended. **Next step:** confirm #2274's
correct date in Paperless (same class of fix as the other three), get an
odometer reading for it, then write it the same way as the others (`POST
/api/vehicles/{vin}/fuel`, tag with 1751+1752).

**Blocked on the MyGarage fork (do not process yet):**
- #2272 (€147.70, Finanzierung/Zinsen — Kfz-Wiederzulassung + Wunschkennzeichen)
- #2273 (€63.80, Finanzierung/Zinsen — Kennzeichen + Umweltplakette, confirmed
  via document content: "Rechnung 233139 – BVA Online Kfz-Zulassung")

Per ticket #17's own correction (comment 356) and #19's identical treatment
of ID.4's #1787: financing/interest costs need the generic cost entity
being built in a maintained fork of `ghcr.io/homelabforge/mygarage`
(tracked in `git.janz.digital/jonas/mygarage#1`, driven directly by Jonas,
its own separate wayfinder map — don't touch that repo). Do not tag these
two documents or run them through the pipeline until that fork's financing
entity exists — doing so now would land them in the generic `documents`
fallback, which is exactly what the ticket says to avoid.

**Still needs Paperless routing before it can be backfilled at all: 2
Google-Photos-linked fuel receipts** (€98.02 and €87.50, per the original
spreadsheet — links were direct Google Photos share URLs, not Paperless
documents). Ticket #17's own resolution names this as a prerequisite step:
these need to become real Paperless documents (via the normal
docling/consume pipeline) before anything else can happen to them. This
wasn't attempted this session (no established Google Photos fetch path) —
either Jonas downloads and uploads them to Paperless himself (tag
`Fahrzeug:Multivan-auto` once in, and the live pipeline will draft them
automatically — no direct-write treatment needed since they'd then go
through the normal review-queue path with a real Paperless doc backing
them), or a future session builds a Google Photos fetch step.

**7 spreadsheet template rows (KFZ-Steuer, Haftpflicht, Kasko, HU/AU-TÜV, a
generic "Tankstopp" placeholder, Reifenwechsel/Einlagerung,
Autowäsche/Pflege) are confirmed genuinely blank** — Jonas confirmed these
per direct interview this session. Nothing to backfill; they'll flow
through the live pipeline naturally whenever they actually occur (the
Multivan's short ownership window means most of these — annual tax, annual
insurance, biennial TÜV — simply haven't come due yet).

## Not yet started

- **#38 (ID.4 charging costs via Zappi)** — independent of everything above.
  A small new ingestion component polling Home Assistant's Zappi session
  sensor (`sensor.myenergi_chargeboi_charge_added_session`), direct-writing
  `fuel` records to MyGarage (no review queue — deterministic sensor data).
  See map #5 / ticket #38 for the full design (flat-rate €/kWh model,
  session-boundary detection via plug-status sensors). No code exists yet.
- **#9/#10 (trip-enricher weekly review + Kilometerstand backfill)** — **not
  implemented**, despite ADRs being merged (`containers/trip-enricher/docs/adr/0001-*.md`,
  `0002-*.md`) and this being reported as "done" earlier in the session that
  led to this one — that report was wrong; verified directly against source
  (`trip_enricher.py` has no HTTP server, no PATCH endpoint, no backfill
  script exists). Independent of the vehicle-cost-pipeline work above.
  Read the two ADRs for the already-decided design before starting.

## Quick reference

- **Tag IDs (Paperless):** `Fahrzeug:ID4-auto` = 1750, `Fahrzeug:Multivan-auto`
  = 1751, `MyGarage:imported` = 1752.
- **VINs:** Multivan T7 = `WV2ZZZST4SH003739`, ID.4 = `WVGZZZE27SE017858`
  (also in Doppler `MYGARAGE_VEHICLES`, `homelab/home`).
- **MyGarage base URL:** `https://mygarage.apps.altus.janz.digital` (public)
  / `http://mygarage-app.mygarage.svc:8686` (in-cluster, use this from
  anything running on Altus).
- **Paperless base URL:** `https://paperless.janz.digital` (outbound calls
  work fine from in-cluster; the *webhook* Paperless calls *into*
  vehicle-pipeline uses in-cluster DNS instead —
  `http://vehicle-pipeline-app-app.vehicle-pipeline.svc.cluster.local:8000/webhooks/paperless-vehicle`,
  confirmed against taxbuddy's own identical live pattern).
- **Paperless workflow:** "Vehicle cost pipeline webhook" (id 3), triggers
  on both Document Added and Document Updated, filters on the two Fahrzeug
  tags above.
- **Credentials reused rather than newly minted** (per Jonas's explicit
  direction): `VEHICLE_PIPELINE_PAPERLESS_TOKEN`/`_URL` = copies of
  taxbuddy's own (`TAXBUDDY_PAPERLESS_TOKEN`/`_URL` — this Paperless
  instance only supports one API token total, not per-integration tokens);
  `VEHICLE_PIPELINE_GHCR_PULL_TOKEN` = copy of `HOME_OPS_PAT`.
- **To check current pipeline health:** `oc logs -n vehicle-pipeline
  deploy/vehicle-pipeline-app --tail=50` (Altus cluster — kubeconfig via
  Doppler `ALTUS_OCP_KUBECONFIG`, `infra-ops/prd`, base64-encoded). To query
  the review queue directly: `oc exec -n vehicle-pipeline
  vehicle-pipeline-db-1 -- psql -U postgres -d vehicle_pipeline -c "SELECT
  ... FROM vehicle_pipeline.review_items"`.
