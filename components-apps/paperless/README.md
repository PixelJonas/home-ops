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
