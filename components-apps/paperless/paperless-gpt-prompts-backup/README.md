# paperless-gpt prompt templates (backup)

Verbatim copies of the prompt templates the retired `paperless-gpt`
deployment (ghcr.io/icereed/paperless-gpt) used, exported before the
deployment was removed (ingestbuddy#9 / #10).

- Source: `/app/prompts` in pod `deploy/paperless-gpt`, namespace `paperless`,
  cluster altus -- i.e. the `paperless-gpt-prompts` PVC.
- Exported: 2026-10-07, read-only (`oc exec ... cat`); md5 checksums of every
  file were verified identical to the live copies.
- File mtimes on the PVC: `document_type_prompt.tmpl` 2026-07-25, all others
  2025-10-02.

These files are documentation only -- nothing in this repo deploys them.

Re-verified 2026-10-16 (ingestbuddy#10): md5 of all 8 files identical in git,
on the PVC, and in the final restic snapshot `0f36e922` of the gpt-prompts
repo.

As of 2026-10-25 (ingestbuddy#10) this directory is the **only** remaining copy:
the `paperless-gpt-prompts` PVC and both paperless-gpt restic repos were
deleted.

The deployment's own configuration (env, models, OCR settings) lived only in
the removed ArgoCD Application's Helm values; see
`git show 84817ad^:components-apps/paperless/paperless-ai-chart-app.yaml`.
