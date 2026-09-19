# actual-budget

[Actual Budget](https://actualbudget.org/) — transaction spine for taxbuddy
(ADR-002 in the taxbuddy repo). Altus-only, internal route, no public ingress.

- Image: `actualbudget/actual-server`, digest-pinned (bump deliberately; monthly
  upstream releases, migrations run on file open).
- Route: `https://actual.apps.altus.janz.digital` (edge TLS, `openshift-default`).
- Data: single PVC `actual-budget-app` at `/data` (`lvms-vg1`, 5Gi).
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
   upload the RSA credential file (taxbuddy repo `spikes/eb-secrets/`, local
   only, gitignored). Consent is capped at 90 days by Actual.

## Operational habits

- **Quarterly: Reset Sync.** The budget file grows unboundedly (every mutation
  is appended to the change log). Settings → Reset Sync compacts history into a
  fresh base file; safe, but re-uploads the file and invalidates old sync
  state. Tracked as a recurring kanban reminder in taxbuddy.
- No E2EE: bank-sync secrets live in `account.sqlite` regardless and E2EE would
  force the encryption password into every automated `downloadBudget`.
  Encryption happens at the infra layer (PVC/backup) instead.
