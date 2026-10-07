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
The `paperless-gpt-prompts` PVC itself is retained (see
`../paperless-gpt-retained-pvcs.yaml`) until ingestbuddy#10 deletes it.
