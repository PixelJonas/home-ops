# Gitea on Altus — Design

**Status:** Approved by user 2026-08-30. Implementation plan: `docs/superpowers/plans/2026-08-30-gitea-deployment.md` (home-ops repo).

## Goal

Deploy Gitea on the Altus OpenShift cluster as the user's primary git host going
forward, reachable both publicly (via the existing cloudflared tunnel) and from
the LAN (directly, bypassing Cloudflare, via AdGuard split-horizon DNS), with a
real Let's Encrypt certificate on the LAN-direct path. Create the user's admin
account and authorize their existing SSH key. Include Gitea Actions with Docker
execution support, reusing this homelab's existing OpenShift privileged-runner
pattern. Back up to the existing in-cluster restic REST server only (no
Backblaze/offsite copy).

## Hostnames & Network Paths

Three independent paths to the same Gitea instance, all using the single
public hostname `git.janz.digital` for the two HTTP paths:

1. **Public web** — browser → `git.janz.digital` (public DNS, Cloudflare
   proxied) → Cloudflare edge (TLS terminated with Cloudflare's own edge
   cert, not Let's Encrypt — this is inherent to Cloudflare proxying and
   matches how every other tunneled app in this repo works) → cloudflared
   (in-cluster Deployment) → Gitea's HTTP Service, plain HTTP inside the
   cluster network.
2. **LAN web** — browser → `git.janz.digital` → **AdGuard DNS rewrite**
   (new entry, same mechanism as the existing `asgard_tailscale`/
   `knowhere_tailscale` split-horizon rewrites) → straight to the OpenShift
   router's IP (`INFRA_UNIFI_DNS_ALTUS_APPS_IP`, reused — Routes are
   SNI-routed at one shared router IP regardless of hostname) → OpenShift
   Route, edge TLS termination, served by the cluster's **default
   IngressController certificate** with `git.janz.digital` added as an
   additional SAN (a real Let's Encrypt cert via the existing
   `cloudflare-issuer` ClusterIssuer, DNS-01). This satisfies "LAN doesn't
   use cloudflared" + "Let's Encrypt."
3. **Git-over-SSH** — `ssh://git@gitea.<tailnet>.ts.net:22` → Tailscale
   Kubernetes Operator-exposed Service (`tailscale.com/expose: "true"`,
   `tailscale.com/hostname: "gitea"` set directly as `service.ssh.annotations`
   on the chart's own SSH `Service` — no separate Service object needed,
   confirmed against the chart's actual `values.yaml` schema) → Gitea's
   built-in SSH server (rootless image, `SSH_LISTEN_PORT: 2222` inside the
   container, chart-managed port mapping to the external `22`). Reachable
   from LAN and anywhere on the tailnet; never exposed publicly.

**Correction from initial research:** the cluster's Altus-targeting overlay
directory is `bootstrap/overlays/local.home/` in home-ops, not `sakaar`
(`sakaar` is a separate, real Hetzner-hosted cluster). All Altus-specific
manifests in this design register under `local.home`. Gitea is deployed
**Altus-only** — a deliberate deviation from home-ops' usual both-clusters
default, since a git host is a singleton service you don't want two
independently-writable copies of.

**Correction from initial research:** rather than a brand-new per-app
`cert-manager` `Certificate`, `git.janz.digital` is added as an additional
`dnsNames` entry on the cluster's existing
`components-infra/certificates/base/openshift-wildcard-certificate.yaml`,
which is already the default IngressController's serving certificate
(`spec.defaultCertificate: openshift-wildcard-certificate` in
`ingresscontroller.yaml`). Every other app's public tunnel hostname
(`vault.janz.digital`, `paperless.janz.digital`, etc.) has no Route at all —
those are pure cloudflared→Service pass-throughs. Gitea's LAN-direct path is
new territory for this repo (first app to have a custom-domain Route
alongside its tunnel path), not a copy of an existing pattern.

## Deployment (home-ops repo)

`components-apps/gitea/`, following `docs/adding-apps.md` conventions as
closely as a non-`app-template` chart allows:

- Official Gitea Helm chart (confirmed real repo/coordinates:
  `repoURL: https://dl.gitea.com/charts`, `chart: gitea`, latest published
  version `1.5.5`, `appVersion 1.27.2`) as the ArgoCD Application source —
  not `app-template` — since Gitea's chart has first-class support for
  external Postgres, a native OpenShift `route.*` block (confirmed in the
  chart's own `values.yaml`, so no need for `app-template`'s Ingress
  wrapper), a dedicated SSH `Service`, and admin bootstrap via
  `gitea.admin.existingSecret`, which would otherwise mean
  hand-reimplementing Gitea's own app.ini/entrypoint logic.
- **Correction from initial research:** the chart's `route.enabled: true` +
  `route.host: git.janz.digital` + `route.tls.termination: edge` (leaving
  `route.tls.certificate`/`key` unset so it falls back to the
  IngressController's default certificate, per the SAN addition above) is
  what creates the LAN-direct Route — no `app-template` `ingress:` block
  involved at all, since this chart is deployed standalone.
- Bundled `postgresql-ha` and `postgresql` subcharts both explicitly
  disabled (`postgresql-ha.enabled: false`, `postgresql.enabled: false`);
  external DB wired via `deployment.env` using Gitea's standard
  `GITEA__database__*` environment-variable override convention (`DB_TYPE`,
  `HOST`, `NAME`, `USER` as plain values, `PASSWD` via `valueFrom.secretKeyRef`
  against the CNPG-generated password Secret).
- CNPG `Cluster` (`postgresql-database.yaml`), single instance, matching the
  `paperless-db` pattern exactly (`bootstrap.initdb` with a Doppler-sourced
  password Secret).
- `/data` (repos, LFS, avatars, config) on `lvms-vg1`, via the chart's own
  `persistence.*` block (not `app-template`'s `persistence:` key).
- Namespace `gitea`, registered in
  `bootstrap/overlays/local.home/values-apps.yaml` only.

## Account Bootstrap

- Admin account (`jonas` / `jonas@famjanz.de`) created automatically by the
  chart via `gitea.admin.existingSecret` pointing at an ExternalSecret-backed
  Secret (`username`, `password`, `email` keys) — no manual Job needed for
  the account itself.
- SSH key authorization: a `PostSync` hook Job calls
  `POST /api/v1/user/keys` once Gitea is reachable, using the admin
  credentials and the user's existing SSH public key.
- **Doppler bridging required:** `PERSONAL_SSH_PUBLIC_KEY` lives in
  `infra-ops/prd`, but home-ops apps use the `doppler-cluster`
  `ClusterSecretStore` (backed by `homelab/home`) — `doppler-infra`
  (backed by `infra-ops/prd`) exists **only on Sakaar**, not Altus. The
  value must be mirrored into a new `homelab/home` secret
  (`GITEA_ADMIN_SSH_PUBLIC_KEY`) before the ExternalSecret can reference
  it. This is a one-time manual step, done via the CLI-with-suppressed-output
  pattern (never through the value-returning Doppler MCP tools).

## Baseline Config

- `DISABLE_REGISTRATION=true` (single-user instance).
- `ROOT_URL` in auto-detect mode, so links resolve correctly regardless of
  which of the two web paths (public vs LAN-direct) a client used.
- `REVERSE_PROXY_TRUSTED_PROXIES` scoped tightly to the cloudflared pod /
  router CIDR — never `*` (real CVE, GHSA-f75j-4cw6-rmx4: a wildcard here
  lets any source spoof the authenticated user via a forged header).

## Actions / CI Runner

**Correction from initial research:** Gitea publishes its own official
`actions` Helm chart (`gitea.com/gitea/helm-actions`, chart repo
`https://dl.gitea.com/charts`, latest `0.1.2`/`appVersion 0.261.3`) purpose
-built for act_runner. Its `StatefulSet` template
(`gitea.com/gitea/helm-actions/src/branch/main/templates/statefulset.yaml`)
already runs a **privileged Docker-in-Docker sidecar** (a K8s 1.28+ init
container with `restartPolicy: Always`, `securityContext.privileged: true`,
sharing a `docker.sock` `emptyDir` with the `runner` container) —
hardcoded, not configurable away. This directly answers the "unverified
gap" the first pass of research flagged: act_runner's Docker executor talks
to the Docker Engine API over a socket, not the CLI, which is exactly why
Gitea ships DinD rather than a CLI-shim image. **The
`gha-runner`/podman-docker-shim approach this homelab built for GitHub
Actions does not transfer here** — it solved a problem (no ready-made
runner chart existed for GitHub) that doesn't exist for Gitea. No custom
Containerfile, no new GHCR image, no new build workflow.

**What does carry over, and is the actual reusable insight:** the
privileged-SCC-grant pattern itself
(`components-infra/gha-runner-scale-set-recordbuddy/base/privileged-clusterrolebinding.yaml`
— a `ClusterRoleBinding` to `system:openshift:scc:privileged` on the
runner pod's ServiceAccount). The official chart's DinD sidecar needs
exactly the same OpenShift admission grant the GitHub runner's DinD/podman
container needed, just applied to a different ServiceAccount.

- Deploy the `actions` chart as its own ArgoCD Application in the `gitea`
  namespace (`repoURL: https://dl.gitea.com/charts`, `chart: actions`),
  alongside the main `gitea` chart Application.
- `giteaRootURL` set to the in-cluster Gitea Service URL (e.g.
  `http://gitea-http.gitea.svc.cluster.local:3000`).
- `statefulset.serviceAccountName` set to a dedicated `gitea-actions-runner`
  ServiceAccount, bound to `system:openshift:scc:privileged` via a new
  `ClusterRoleBinding` (namespace-scoped blast radius, matching the ARC
  precedent).
- `existingSecret`/`existingSecretKey` point at a Secret populated by a
  one-time bootstrap step: call
  `POST /api/v1/admin/actions/runners/registration-token` (confirmed
  against Gitea's own source, `routers/api/v1/admin/runners.go` /
  `api.go` route table — global, instance-level token,
  admin-authenticated) using the admin credentials from the same
  ExternalSecret as the chart's own admin bootstrap, and write the
  returned token into the Secret the chart reads.

## Backup

Local-restic only — **no Backblaze/B2 template** (explicit user choice,
overriding this repo's usual dual-target default). Uses the existing
in-cluster `restic-rest-server` (namespace `restic-rest`, NFS-backed,
already used by every other app) via the `templates/volsync/rest`
`ReplicationSource` template — `templates/volsync/backblaze` is omitted
entirely for both the `/data` volume and the CNPG database (`pg_dump` via
`templates/psql-backup`, restic destination only).

**Caveat surfaced, not hidden:** this is on-site only. A NAS-level failure
takes out Gitea's live data and its only backup simultaneously — a real
tradeoff against B2's offsite copy, accepted here per explicit user
instruction.

## Terraform / Ansible Changes (infra-ops repo)

- `stacks/cloudflare/doppler.tf` — new local `tunnel_svc_gitea` mapped to
  new Doppler secret `CF_TUNNEL_SVC_GITEA`.
- `stacks/cloudflare/tunnels.tf` — new ingress rule on the `k8s` tunnel
  (`hostname = "git.${local.zone_name}"`, `service = local.tunnel_svc_gitea`),
  inserted before the trailing `http_status:404` catch-all.
- `stacks/cloudflare/dns.tf` — new entry in `local.tunnel_cname_records`
  (`gitea = { record_id = null, tunnel_id = local.tunnel_k8s_id }` — new
  record, no import needed) — **note:** the CNAME record name is `gitea`
  even though the tunnel ingress hostname is `git.${local.zone_name}`; the
  `dns.tf` map key must be `git`, not `gitea`, to produce a matching
  `git.janz.digital` A/CNAME record. Use key `git`.
- `stacks/unifi/ansible/roles/udm_adguard/tasks/main.yml` — new entry in
  `agh_desired_rewrites`: domain from new Doppler secret
  `INFRA_UNIFI_DNS_GITEA_DOMAIN` (value `git.janz.digital`), answer reuses
  the **existing** `DNS_ALTUS_APPS_IP` env var (no new IP secret needed,
  since Routes share one router IP regardless of hostname).
- `mise.toml` — both `unifi-udm:check` and `unifi-udm:apply` tasks
  hardcode their exported env var list; add
  `DNS_GITEA_DOMAIN=$(doppler secrets get INFRA_UNIFI_DNS_GITEA_DOMAIN ...)`
  and add `DNS_GITEA_DOMAIN` to the `export` line, in both tasks.

## Security Notes

- `REVERSE_PROXY_TRUSTED_PROXIES` scoping (see Baseline Config above).
- SSH key bootstrap Job's admin credentials come from the same
  ExternalSecret as the chart's own admin bootstrap — no separate
  credential to manage or leak.
- act_runner's `privileged: true` grant is scoped to the `gitea` namespace's
  `default` ServiceAccount only, matching the existing ARC precedent's
  blast radius (namespace-scoped SCC binding, not cluster-default).

## Explicitly Out of Scope

- Sakaar deployment (Altus-only, singleton service).
- Backblaze/B2 backup (explicit user choice).
- Any migration of existing GitHub repos into Gitea (not requested).
