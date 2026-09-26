# ingestbuddy

Unified document-ingestion pipeline (repo: `PixelJonas/ingestbuddy`). ArgoCD
Application `ingestbuddy-app` (bjw-s app-template) with controllers `api`
(FastAPI intake, `pipeline_api` image), `worker` (RQ consumer,
`pipeline_worker` image), and `valkey`. Deliberately no `agent-runtime` or
`web-ui` controller (infra-ops#61) — those stay taxbuddy's until a later
cutover phase.

Deployment layout is a verbatim mirror of `components-apps/taxbuddy/`'s
two-level pattern: namespace (wave 0), RBAC/ExternalSecrets (wave 30), CNPG
`ingestbuddy-db` (wave 100), migration Job (wave 105), the app-template
Application (wave 111), registered in
`bootstrap/overlays/local.home/values-apps.yaml` (wave 300). Altus
(`local.home`) only — this stack (docling-serve, litellm, qdrant, taxbuddy)
has no Sakaar presence.

## Known gaps / provisional wiring

Two pieces of config here are placeholders, deliberately, because the thing
they point at doesn't exist yet:

- **`TAXBUDDY_INTAKE_URL`** (worker env, `ingestbuddy-app.yaml`) points at
  taxbuddy's real in-cluster `ingestion-api` Service
  (`taxbuddy-app-ingestion-api.taxbuddy.svc.cluster.local:8000`) plus a
  guessed path (`/webhooks/ingestbuddy`) — taxbuddy's actual intake endpoint
  (taxbuddy#109) is still open, with no path decided yet. Every dispatch to
  the `taxbuddy` sink will fail until #109 ships; that failure is caught
  per-bundle by `dispatch_to_sink` and recorded as a failed `SinkDispatch`,
  not a worker crash, so this does not block the rest of the pipeline.
  **Update this URL (and verify the path) once #109 lands.**
- **`TAXBUDDY_WEBHOOK_SECRET`** (`external-ingestbuddy-credentials.yaml`,
  Doppler key `TAXBUDDY_INGESTBUDDY_WEBHOOK_SECRET`) was generated fresh by
  this ticket (#6) since there is nothing on the taxbuddy side to match yet.
  Named following taxbuddy's own `TAXBUDDY_<source>_WEBHOOK_SECRET`
  convention (see `TAXBUDDY_PAPERLESS_WEBHOOK_SECRET`) so whoever implements
  #109 finds and reuses this value via `hmac.compare_digest` rather than
  minting a second, mismatched one.
- **Image digests** (`api`/`worker` controllers, `migration-job.yaml`) are
  `sha256:` placeholders — every `release.yml` run on the ingestbuddy repo
  to date is `cancelled`, not `success`, so no real image has been pushed to
  `ghcr.io/pixeljonas/ingestbuddy/*` yet. `release.yml` only regex-bumps
  `ingestbuddy-app.yaml`'s `repository:`/`digest:` pairs, **not**
  `migration-job.yaml`'s — unlike taxbuddy's equivalent, which keeps both in
  sync in the same auto-merging PR. Recommend extending `release.yml` (in
  the ingestbuddy repo) to also bump `migration-job.yaml` once real images
  exist, so the migration Job doesn't silently drift behind the app image.
- **`migration-job.yaml`'s migration command** runs alembic programmatically
  (`Config().set_main_option("script_location", ...)` against the installed
  `ingest_core.migrations` package) instead of the CLI + `-c alembic.ini`,
  because `packages/ingest_core/alembic.ini` is a project-root file, not
  part of the installed Python package, so it never lands in the built
  image. taxbuddy solves the equivalent problem with a real
  `tax_core.migrations.__main__` CLI entrypoint (taxbuddy#107); ingestbuddy
  has no analogous entrypoint yet. Recommend adding
  `python -m ingest_core.migrations upgrade head` to the ingestbuddy repo as
  a fast-follow so this Job can shed the inline script.

## Backup (Doppler `homelab`/`home` via ESO)

`backup/database/` is `templates/psql-backup` + `templates/volsync/rest`
only — **no Backblaze/B2** (infra-ops#61: Valkey is an ephemeral queue, not
backed up; the CNPG Postgres is the only state, since original document
bytes live in `ingest.bundles.original_file` bytea with no object storage).
New Doppler key `INGESTBUDDY_DATABASE_REST_REPO_LOCAL`; shared
`RESTIC_PASSWORD` reused (same as every other app's REST backup).

## Secrets

| Key | Purpose |
|-----|---------|
| `INGESTBUDDY_POSTGRES_PASSWORD` | CNPG `ingestbuddy-db` password; templated into `ingestbuddy-db-connection`'s `uri` |
| `INGESTBUDDY_GHCR_PULL_TOKEN` | GHCR pull secret for `ghcr.io/pixeljonas/ingestbuddy/*` |
| `INGESTBUDDY_PAPERLESS_WEBHOOK_SECRET` | Verifies incoming Paperless webhook calls (ticket #5, not yet consumed by code) |
| `INGESTBUDDY_DOCLING_SERVE_API_KEY` | docling-serve auth. **Same value as `TAXBUDDY_DOCLING_SERVE_API_KEY`** — docling-serve's own `DOCLING_SERVE_API_KEY` env is a single shared value the server checks against (upstream docling-serve has no multi-key support), so this must stay byte-for-byte identical to whatever `docling-serve-credentials`/`DOCLING_SERVE_API_KEY` actually holds. |
| `INGESTBUDDY_LITELLM_KEY` | ingestbuddy's own LiteLLM virtual key |
| `INGESTBUDDY_DATABASE_REST_REPO_LOCAL` | restic REST repo for the Postgres backup |
| `INGESTBUDDY_PAPERLESS_PUBLIC_URL` | External Paperless route, used only to build human-clickable doc links (`TaxbuddySink.doc_url`) — copy of `TAXBUDDY_PAPERLESS_URL`'s value |
| `TAXBUDDY_INGESTBUDDY_WEBHOOK_SECRET` | See "Known gaps" above |
| `PAPERLESS_API_TOKEN` | Shared, app-neutral (infra-ops#61) — same value as `TAXBUDDY_PAPERLESS_TOKEN`; taxbuddy migrates to this key at cutover (#9) and `TAXBUDDY_PAPERLESS_TOKEN` retires |
| `QDRANT_API_KEY` | Shared — the one QDrant instance's one API key (`components-apps/qdrant`) |
