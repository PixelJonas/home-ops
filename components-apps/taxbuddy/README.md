# taxbuddy

German tax-assistant platform (repo: `PixelJonas/taxbuddy`). ArgoCD Application
`taxbuddy-app` (bjw-s app-template) with controllers: `ingestion-api`,
`doc-pipeline`, `agent-runtime`, `valkey`, and the `sync-worker` CronJob.

## Database migrations (HARD-T01)

`migration-job.yaml` is a sync-wave-gated Job (`taxbuddy-db-migrate`, wave 105 —
after the CNPG cluster at wave 100 and the `taxbuddy-db-connection` secret at
wave 30, before the `taxbuddy-app` Application CR at wave 111) that runs
`python -m tax_core.migrations upgrade head` from the pinned `ingestion_api`
image on every sync. Properties:

- **Idempotent:** `alembic upgrade head` is a no-op when the DB is already at
  head.
- **Failure-loud:** ArgoCD waits for each wave to become healthy before moving
  on, and a Job is only healthy once it completes — so a failed migration fails
  the sync at wave 105, the updated `taxbuddy-app` spec (wave 111) is never
  applied, and the rollout is blocked with the parent `taxbuddy` Application
  Degraded.
- **Version-locked:** the Job's image digest is bumped by taxbuddy's
  `release.yml` in the same auto-merging PR as the app controllers, so the
  migrations applied always match the code being rolled out.
- **Not a PreSync hook, on purpose:** hook phase takes precedence over sync
  waves in ArgoCD's ordering, so a PreSync hook would run before the CNPG
  cluster exists on a fresh deploy and deadlock the first sync. The Job is a
  regular wave-105 resource with `sync-options: Replace=true` (Job pod
  templates are immutable — the digest bump would otherwise fail every sync).
  `ttlSecondsAfterFinished` keeps the last run inspectable for a week; if the
  TTL cleaner deletes it, selfHeal recreates it and the no-op re-run succeeds.

The Job needs an `ingestion_api` image built from a taxbuddy commit containing
the `tax_core.migrations` entrypoint (taxbuddy#107); older digests fail it
with `No module named tax_core.migrations.__main__`.

### Manual escape hatch

Run Alembic by hand — e.g. to unblock a rollout whose migration Job keeps
failing, to inspect state (`current`, `history`), or to apply a deliberate
`downgrade`:

```bash
oc apply -n taxbuddy -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: taxbuddy-db-migrate-manual
spec:
  backoffLimit: 0
  template:
    spec:
      serviceAccountName: taxbuddy-db-migrate
      restartPolicy: Never
      imagePullSecrets:
        - name: taxbuddy-ghcr-pull-secret
      containers:
        - name: migrate
          # Use the digest pinned in taxbuddy-app.yaml (or migration-job.yaml)
          image: "ghcr.io/pixeljonas/taxbuddy/ingestion_api@<DIGEST>"
          command: ["python", "-m", "tax_core.migrations", "upgrade", "head"]
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: taxbuddy-db-connection
                  key: uri
EOF
oc logs -n taxbuddy -f job/taxbuddy-db-migrate-manual
```

Swap `upgrade head` for `current`, `stamp head`, `downgrade <rev>`, etc. as
needed (all Alembic args pass through). Once the DB is at head the wave-105
migration Job succeeds again and the blocked sync proceeds. Clean up with
`oc delete job taxbuddy-db-migrate-manual -n taxbuddy`.

If the migration Job itself is permanently broken (not the migration), remove
`migration-job.yaml` from `kustomization.yaml` in a PR and the parent app prunes
it — but that re-opens the "production drifts behind on migrations" gap HARD-T01
closed, so prefer fixing forward.


## sync-worker (Actual Budget → tax.transactions mirror, P2-T04 / ADR-0020)

Daily CronJob `taxbuddy-app-sync-worker` at 05:15 Europe/Berlin (offset from the
actual-budget volsync backup at 05:00). One-shot pod from
`ghcr.io/pixeljonas/taxbuddy/sync_worker` (digest pinned; taxbuddy's
`release.yml` auto-bumps the `repository:` + `digest:` pair in
`taxbuddy-app.yaml` on every taxbuddy main build via an auto-merging PR).

Per run, per active `enable_banking` bank connection: Actual bank-sync trigger →
one wide transaction pull since the cursor → idempotent upsert into
`tax.transactions` → `import_runs` row → `bank_connections` status update. The
job exits non-zero if any bank failed. Actual is the write authority — the
worker never writes back.

### One-time setup (owner)

The CronJob is deliberately gated on the Actual budget existing:

1. Finish the Actual bootstrap in `components-apps/actual-budget/README.md`
   (password, budget file, enableBanking flag, bank linking).
2. Copy the budget's Sync ID (Actual UI: Settings → Advanced) into Doppler
   `homelab`/`home` as **`TAXBUDDY_ACTUAL_SYNC_ID`**. Until this key exists,
   ExternalSecret `external-taxbuddy-actual-sync` stays in error state and the
   CronJob pod cannot start — that's intentional.
3. Insert one `tax.bank_connections` row per linked bank with
   `channel='enable_banking'`, `status='active'`, and
   `meta->>'actual_account_id'` set to the Actual account UUID (Actual UI:
   account settings, or `actual accounts list` via the CLI image).

### Manual re-trigger

```bash
oc create job --from=cronjob/taxbuddy-app-sync-worker \
  taxbuddy-sync-manual-$(date +%s) -n taxbuddy
```

Re-runs are idempotent (upsert keyed on `actual_id`); `import_runs` records
`imported_count` vs `duplicate_count`.

## Secrets (Doppler `homelab`/`home` via ESO)

| Key | Purpose |
|-----|---------|
| `TAXBUDDY_POSTGRES_PASSWORD` | CNPG `taxbuddy-db` password; templated into `taxbuddy-db-connection` `uri` |
| `TAXBUDDY_PAPERLESS_URL` / `TAXBUDDY_PAPERLESS_TOKEN` / `TAXBUDDY_PAPERLESS_WEBHOOK_SECRET` | Paperless ingest |
| `TAXBUDDY_LITELLM_BASE_URL` / `TAXBUDDY_DOCPIPELINE_LITELLM_KEY` / `TAXBUDDY_LLMCLIENT_LITELLM_KEY` | LLM proxy |
| `TAXBUDDY_DOCLING_SERVE_API_KEY` | docling-serve |
| `TAXBUDDY_DOCPIPELINE_HONCHO_API_KEY` | Honcho memory |
| `ACTUAL_BUDGET_PASSWORD` | Actual server password, file-mounted for the sync worker |
| `TAXBUDDY_ACTUAL_SYNC_ID` | Actual budget sync ID (manual, post budget creation) |
