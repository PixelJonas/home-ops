# Actual Budget — operations runbook

Actual Budget (`components-apps/actual-budget/`) is the taxbuddy transaction
spine (taxbuddy ADR-002; deploy ticket PixelJonas/taxbuddy#86). Single
container, SQLite state on one PVC, no Postgres.

- **URL (internal only):** https://actual-budget.apps.altus.janz.digital
- **Namespace:** `actual-budget` (altus / local.home only — not on sakaar)
- **Image:** `actualbudget/actual-server`, tag + digest pinned in
  `actual-budget-app.yaml` (Renovate bumps the tag; bump the digest with it
  and read the release notes — the Enable Banking integration is flagged
  experimental and can change shape between monthly releases)
- **Data:** PVC `actual-budget-app-data` at `/data` (`server-files/` +
  `user-files/`), backed up via volsync to the in-cluster restic REST server
  and offsite to Backblaze B2 (`volsync-actual-budget-data` bucket)

## One-time setup (owner-manual, not automatable)

1. **Set the server password.** Open the URL; on first boot Actual asks for a
   password. Set it to the value of Doppler key `ACTUAL_BUDGET_PASSWORD`
   (homelab/home). Actual has no env/config to preset the password (verified
   against `packages/sync-server/src/load-config.js` at v26.9.0) — it's stored
   hashed in `server-files/account.sqlite`. The same value is also in the
   `actual-budget-credentials` K8s Secret for later API integrations.
2. **Create the budget file** in the UI. State lands on the PVC; verify
   persistence with a pod restart (`oc delete pod -n actual-budget -l ...`)
   if you want to be thorough.
3. **Enable the experimental Enable Banking flag.** Per-budget UI setting,
   not server-configurable: inside the budget, **Settings → Experimental →
   Enable Banking** (`enableBanking` feature flag).
4. **Register the auth callback on the Enable Banking application.** In the
   EB dashboard, on the existing "taxbuddy" application (App ID
   `5dc31083-c482-40fd-aea9-039d76d03765`), add allowed redirect URL:
   `https://actual-budget.apps.altus.janz.digital/enablebanking/auth_callback`
   (append — do not remove the existing firefly/eb-spike URL until the new
   flow is proven).
5. **Link banks in Actual:** More → Bank Sync → Set up Enable Banking → paste
   the App ID (Doppler `TAXBUDDY_EB_APP_ID` — also in the
   `actual-budget-credentials` Secret as `EB_APP_ID`) and upload the RSA
   private key file (Doppler `TAXBUDDY_EB_PRIVATE_KEY` / Secret key
   `EB_PRIVATE_KEY`). Note: Actual caps consent at 90 days, so each bank
   needs re-linking ~4×/year.
6. **Historical import advice (from research #52):** start from an opening
   balance at a recent date, then sync — don't pull maximal history (PSD2
   ~90-day horizon after the first hour anyway).

## Recurring: quarterly Reset Sync

Actual's budget file grows unboundedly — every mutation is appended to a
CRDT change log, and every new client's first download replays the whole
log. The documented remedy is **Settings → Reset Sync** (or
export/re-import), which compacts history into a fresh base file. Do this
**quarterly**; it's safe but re-uploads the file and invalidates old sync
state, so do it when no API sync is mid-flight.

> **Follow-up (not yet done):** this should surface as a recurring kanban
> reminder in the taxbuddy system. Reminder creation is tracked as a
> follow-up to taxbuddy#86 — the kanban lives in taxbuddy, not in this repo.

## Backups & restore

Volsync `ReplicationSource`s `data-backup` (restic REST server, in-cluster)
and `data-backup-b2` (Backblaze B2) run daily at 05:00, sync-wave 200,
`copyMethod: Snapshot`. Restore follows the same pattern as
`docs/runbooks/nextcloud-restore.md` (ReplicationDestination from the restic
repo). App-level export (Settings → Export, zip) is a second, portable
escape hatch.

## Notes

- **No E2EE** (deliberate): E2E encryption covers only the budget file, not
  the bank-sync secrets in `account.sqlite`, and it would force every API
  `downloadBudget` to carry the encryption password. PVC/backup encryption
  is handled at the infra layer instead.
- **Auth:** password login (default). `header`/`openid` login methods exist
  upstream but are not enabled.
- **Route timeout** is raised to 300s — first-download change-log replay and
  bank-sync long-polling exceed the 30s default (same failure mode as the
  eb-spike 504s).
- The throwaway **`eb-spike`** Firefly III + FIDI namespace from the P0 spike
  was already gone from altus as of 2026-09-19 (verified with
  `oc get ns | grep -iE 'eb|firefly|spike'` — no matches). Nothing to tear
  down; the valuable state (EB application + bank consents) lives on Enable
  Banking's side.
