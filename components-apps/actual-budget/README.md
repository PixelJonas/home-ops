# actual-budget

[Actual Budget](https://actualbudget.org/) — transaction spine for taxbuddy
(ADR-002 in the taxbuddy repo). Altus-only, internal route, no public ingress.

- Image: `actualbudget/actual-server`, digest-pinned (bump deliberately; monthly
  upstream releases, migrations run on file open).
- Route: `https://actual.apps.altus.janz.digital` (edge TLS, `openshift-default`).
- Data: single PVC `actual-budget-app-data` at `/data` (`lvms-vg1`, 5Gi).
  SQLite only — server state in `/data/server-files/account.sqlite`, budget
  blobs in `/data/user-files/`.
- Backups: volsync restic, in-cluster REST server + Backblaze B2 offsite,
  daily 05:00, snapshot copy method (sync-wave 200).
- SCC: `anyuid` ClusterRoleBinding — the image has no `USER` directive and the
  pod is pinned to `runAsUser/fsGroup 1001` (the image's `actual` user).

## Secrets (Doppler `homelab`/`home` via ESO)

| Key | Purpose |
|-----|---------|
| `ACTUAL_BUDGET_PASSWORD` | Server password. Source of truth — Actual has no env var to preset it, so set this value interactively on first UI visit. Later consumed by the taxbuddy sync-worker (ADR-0020). |
| `TAXBUDDY_EB_APP_ID` | Enable Banking App ID (P0-T02), synced into the `actual-budget-credentials` Secret as `EB_APP_ID` |
| `TAXBUDDY_EB_PRIVATE_KEY` | Enable Banking RSA private key (P0-T02), synced as `EB_PRIVATE_KEY` |
| `ACTUAL_BUDGET_DATA_REST_REPO_LOCAL` | Restic REST repository URL |
| `ACTUAL_BUDGET_DATA_REPO` | Backblaze B2 (S3) repository URL |

`RESTIC_PASSWORD`, `BACKBLAZE_KEY_ID`, `BACKBLAZE_KEY_SECRET` are shared
cluster-wide keys.

## Post-deploy bootstrap (manual, one time)

1. Visit `https://actual.apps.altus.janz.digital`, set the server password to
   the value of `ACTUAL_BUDGET_PASSWORD` (first visit bootstraps the password).
2. Create the budget file.
3. Settings → Experimental: enable the **`enableBanking`** flag (per-client UI
   toggle, no server-side config mechanism exists).
4. Register `https://actual.apps.altus.janz.digital/enablebanking/auth_callback`
   as an allowed redirect URL on the "taxbuddy" Enable Banking application
   (App ID `5dc31083-c482-40fd-aea9-039d76d03765`) in the EB dashboard.
5. In Actual: More → Bank Sync → Set up Enable Banking → paste the App ID and
   upload the RSA credential file. Both are in the `actual-budget-credentials`
   K8s Secret (`EB_APP_ID` / `EB_PRIVATE_KEY`, sourced from Doppler
   `TAXBUDDY_EB_APP_ID` / `TAXBUDDY_EB_PRIVATE_KEY`); a local-only copy also
   lives in the taxbuddy repo `spikes/eb-secrets/` (gitignored). Consent is
   capped at 90 days by Actual.

## Operational habits

- **Quarterly: Reset Sync.** The budget file grows unboundedly (every mutation
  is appended to the change log). Settings → Reset Sync compacts history into a
  fresh base file; safe, but re-uploads the file and invalidates old sync
  state. Follow-up (not yet done): surface this as a recurring kanban
  reminder in the taxbuddy system — the kanban lives in taxbuddy, so the
  reminder is created there, not in this repo.
- No E2EE: bank-sync secrets live in `account.sqlite` regardless and E2EE would
  force the encryption password into every automated `downloadBudget`.
  Encryption happens at the infra layer (PVC/backup) instead.
- The Route timeout is raised to 300s — first-download change-log replay and
  bank-sync long-polling exceed the 30s default (same failure mode as the
  eb-spike 504s, see taxbuddy `docs/spikes/p0-banking.md`).

## eb-spike teardown (taxbuddy#86 follow-up)

The throwaway `eb-spike` Firefly III + FIDI namespace from the P0 spike was
already absent from altus as of 2026-09-19 — verified with
`oc get ns | grep -iE 'eb|firefly|spike'` (no matches) before this app was
deployed. Nothing to tear down; the durable state (EB application + bank
consents) lives on Enable Banking's side, not in that namespace.
