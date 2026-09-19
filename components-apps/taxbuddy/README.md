# taxbuddy

German tax-assistant platform (repo: `PixelJonas/taxbuddy`). ArgoCD Application
`taxbuddy-app` (bjw-s app-template) with controllers: `ingestion-api`,
`doc-pipeline`, `agent-runtime`, `valkey`, and the `sync-worker` CronJob.

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
