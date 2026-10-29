# happier

[happier](https://github.com/happier-dev/happier) relay server (light flavor:
SQLite + local files, embedded web UI) for driving the knowhere coding agents
from the phone. **Evaluation**, running alongside Moshi — see infra-ops
`docs/agents/happier-knowhere-eval-plan-2026-10-29.md` (plan) and
`docs/agents/happier-research-2026-10-29.md` (facts). Altus-only.

- Image: `ghcr.io/happier-dev/relay-server`, `stable` tag pinned by digest.
- In-cluster: `http://happier.happier.svc.cluster.local:3005`.
- Route: `https://happier.apps.altus.janz.digital` (edge TLS, WebSocket tunnel
  timeout 1h for `/v1/updates`).
- Clients use the LAN-only `https://happier.app.janz.digital` (NAS Caddy vhost
  in infra-ops `INFRA_PROXY_HOSTS`, `auth: false` — Authelia `forward_auth`
  can't front bearer-token/Socket.IO clients). That URL is the
  `HAPPIER_PUBLIC_SERVER_URL`.
- Data: PVC `happier-app-data` at `/data` (`lvms-vg1`, 5Gi), `emptyDir` at
  `/tmp`. One replica, `Recreate`.
- SCC: plain `restricted-v2` (arbitrary UID + namespace fsGroup). `anyuid` is
  the fallback if upstream #211 (non-group-writable image dirs) bites.
- Backups: volsync restic, in-cluster REST server + Backblaze B2, daily 05:00,
  snapshot copy method (sync-wave 200).

## Lock signup after the first account

`AUTH_ANONYMOUS_SIGNUP_ENABLED` is `"true"` for the first deploy. Once the
phone account exists and the knowhere daemon is paired, set it to `"false"` in
`happier-app.yaml` and let ArgoCD roll it out. Verify a fresh client gets
`signup-disabled` while the phone still syncs. Existing-key logins, token
refresh and CLI pairing keep working.

## Secrets (Doppler `homelab`/`home` via ESO)

| Key | Purpose |
|-----|---------|
| `HAPPIER_MASTER_SECRET` | Server master secret, synced as `HANDY_MASTER_SECRET` into Secret `happier-credentials`. Must stay with the backups — a restored `/data` needs the same value. |
| `HAPPIER_DATA_REST_REPO_LOCAL` | Restic REST repository URL (`rest:http://restic-rest-server-app.restic-rest:8000/happier-data/`) |
| `HAPPIER_DATA_REPO` | Backblaze B2 (S3) repository URL (bucket `volsync-happier-data`) |

`RESTIC_PASSWORD`, `BACKBLAZE_KEY_ID`, `BACKBLAZE_KEY_SECRET` are shared
cluster-wide keys.
