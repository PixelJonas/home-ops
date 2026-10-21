# paperless

Paperless-ngx (ArgoCD Application `paperless-ng`, bjw-s app-template) in
namespace `paperless`.

## Consume share layout (ingestbuddy#9)

The NFS share `\\statesman\configs\paperless\consume`
(`10.10.10.40:/volume1/configs/paperless/consume`, PV
`pv-paperless-consume` / PVC `nfs-paperless-consume`) is split:

| Subdirectory | Consumer |
|---|---|
| `inbox/` (and `inbox/vehicle/`) | ingestbuddy's NFS poller (`components-apps/ingestbuddy`). **Scans go to `\\statesman\configs\paperless\consume\inbox`.** ingestbuddy processes them and uploads to Paperless via the REST API. |
| `legacy/` | Paperless' `/consume` (`PAPERLESS_CONSUMPTION_DIR`), mounted with `subPath: legacy` in every container |

Paperless' own folder watcher is switched off with
`PAPERLESS_CONSUMER_DISABLE` (stops only the s6 `svc-consumer` /
`document_consumer` process; API uploads and mail fetching are unaffected).
The `legacy/` subPath is a second guard so that, even if the watcher were
re-enabled, Paperless could never race ingestbuddy for files in `inbox/`.

Rollback: revert the PR that introduced this. The NAS directories are
harmless on their own; after a revert Paperless watches the whole share
again (including `inbox/`, if `PAPERLESS_CONSUMER_RECURSIVE` is set).

## MCP read access: `mcp-readers` view grant

The claude.ai Paperless MCP connector (`components-apps/paperless-mcp`) reads
documents as the Paperless user `mcp-readonly`, which is in the group
`mcp-readers` (view-only model permissions: document, tag, correspondent,
document type, storage path, custom field, UI settings). Unowned objects are
visible to every user; objects that have an owner are visible to the group
only when it holds an explicit view grant.

The workflow `mcp:grant view to mcp-readers`
(`workflows/mcp-view-group.workflow.json`, applied by the
`paperless-workflows-apply` Job, order 4) adds that grant to every newly added
document (consume folder, API upload, mail fetch, web UI).

- The group `mcp-readers` is declared in `workflows/mcp-groups.json` and
  upserted by `apply.py`: created if missing, and its permission set is
  drift-corrected on every run, so the read-only guarantee can't be widened by
  hand. A group a workflow references but `mcp-groups.json` doesn't declare is
  a hard error, never an implicit create.
- The user `mcp-readonly` is **not** in git: creating it needs a password and
  its API token lives in Doppler (`homelab/home` `PAPERLESS_MCP_API_TOKEN`).
  Recreate it by hand as a non-staff, non-superuser member of `mcp-readers`
  with no direct permissions, mint a token, and update that Doppler key.
- Existing documents, tags and correspondents were bulk-granted once on
  2026-10-13 (`set_permissions`, `merge: true`).
- **Gap:** tags, correspondents, document types and storage paths created
  *after* that date with an owner are not covered by this workflow (Paperless
  has no "object added" trigger for them). The connector then simply does not
  see that object until the group is granted view on it (UI: object
  permissions, or `POST /api/bulk_edit_objects/` with `set_permissions`).
