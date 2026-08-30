# Gitea on Altus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Gitea on Altus as the primary git host — public via the existing cloudflared tunnel, LAN-direct via AdGuard split-DNS with a real Let's Encrypt cert, git-over-SSH via Tailscale, admin account + SSH key authorized, Actions enabled, backups to the local restic REST server only.

**Architecture:** Two repos. `infra-ops` (Terraform + Ansible) adds the Cloudflare tunnel ingress/DNS record and the AdGuard split-DNS rewrite. `home-ops` (GitOps/ArgoCD) adds the `gitea` namespace with two Helm-chart-backed ArgoCD Applications (official `gitea` chart + official `actions` chart), a CNPG Postgres cluster, ExternalSecrets, local-restic-only VolSync backups, and a PostSync bootstrap Job that authorizes the SSH key and mints the Actions runner registration token. Both repos deploy via `git push` to `main` (no PR gate observed in either repo's recent history) — ArgoCD auto-syncs `home-ops` on push; `infra-ops` changes are applied locally via `mise run terraform:apply:*` / `mise run unifi-udm:apply`.

**Tech Stack:** Terraform (Cloudflare, UniFi/Ansible), Kustomize + ArgoCD (bjw-s is NOT used here — official `gitea`/`actions` Helm charts direct), CloudNativePG, External Secrets Operator + Doppler, VolSync + restic, Tailscale Kubernetes Operator, cert-manager.

**Spec:** `docs/superpowers/specs/2026-08-30-gitea-deployment-design.md` (this repo)

## Global Constraints

- Altus-only. No `sakaar` overlay for this app.
- Backups: local restic REST server only. Never add a `templates/volsync/backblaze` reference for Gitea.
- Every new Doppler secret in `homelab/home` goes through `doppler-cluster`; every new Doppler secret in `infra-ops/prd` stays in `infra-ops`'s own Terraform locals — home-ops apps never reference `infra-ops/prd` directly (no `doppler-infra` on Altus).
- Never use the value-returning Doppler MCP tools (`secrets_get`, `secrets_list`, `secrets_download`, `secrets_update`, `config_logs_get`). CLI only, output suppressed, verified via `wc -c`.
- `git.janz.digital` is the one public hostname for both HTTP paths. Tailscale hostname is `gitea` (→ `gitea.<tailnet>.ts.net`) for SSH only.
- Chart coordinates, pinned: `gitea` chart `repoURL: https://dl.gitea.com/charts`, `chart: gitea`, `targetRevision: 1.5.5`; `actions` chart same `repoURL`, `chart: actions`, `targetRevision: 0.1.2`.

---

### Task 1: Doppler secrets bootstrap

**Files:** none (Doppler only, via CLI).

**Interfaces:**
- Produces (secrets later tasks read via ExternalSecret/Terraform `nonsensitive(data.doppler_secrets...)`):
  - `homelab/home`: `GITEA_ADMIN_PASSWORD`, `GITEA_ADMIN_SSH_PUBLIC_KEY`, `GITEA_POSTGRES_USER_PASSWORD`, `GITEA_DATA_REST_REPO_LOCAL`, `GITEA_DATABASE_REST_REPO_LOCAL`
  - `infra-ops/prd`: `CF_TUNNEL_SVC_GITEA`, `INFRA_UNIFI_DNS_GITEA_DOMAIN`

- [ ] **Step 1: Generate and write the admin password and Postgres password**

```bash
cd /home/jonas/projects/home-ops
source .env.doppler
ADMIN_PW=$(openssl rand -base64 24 | tr -d '=+/' | head -c 32)
PG_PW=$(openssl rand -base64 24 | tr -d '=+/' | head -c 32)
doppler secrets set GITEA_ADMIN_PASSWORD="$ADMIN_PW" -p homelab -c home -t "$DOPPLER_TOKEN" > /dev/null 2>&1; echo "admin rc=$?"
doppler secrets set GITEA_POSTGRES_USER_PASSWORD="$PG_PW" -p homelab -c home -t "$DOPPLER_TOKEN" > /dev/null 2>&1; echo "pg rc=$?"
unset ADMIN_PW PG_PW
```

(home-ops's own `.env.doppler` provides a `homelab/home`-scoped `$DOPPLER_TOKEN` per its own bootstrap convention — confirm the exact var name in `home-ops/.env.doppler` before running; if it differs, substitute it.)

- [ ] **Step 2: Bridge the personal SSH public key from infra-ops/prd into homelab/home**

```bash
cd /home/jonas/projects/infra-ops
source .env.doppler
INFRA_TOKEN="$DOPPLER_TOKEN"
cd /home/jonas/projects/home-ops
source .env.doppler
HOMELAB_TOKEN="$DOPPLER_TOKEN"
doppler secrets get PERSONAL_SSH_PUBLIC_KEY --plain -p infra-ops -c prd -t "$INFRA_TOKEN" \
  | { read -r KEY; doppler secrets set GITEA_ADMIN_SSH_PUBLIC_KEY="$KEY" -p homelab -c home -t "$HOMELAB_TOKEN" > /dev/null 2>&1; echo "rc=$?"; }
unset INFRA_TOKEN HOMELAB_TOKEN
```

- [ ] **Step 3: Add the local-restic backup repo URLs**

```bash
cd /home/jonas/projects/home-ops
source .env.doppler
doppler secrets set GITEA_DATA_REST_REPO_LOCAL="rest:http://restic-rest-server-app.restic-rest:8000/gitea-app-data/" -p homelab -c home -t "$DOPPLER_TOKEN" > /dev/null 2>&1; echo "rc=$?"
doppler secrets set GITEA_DATABASE_REST_REPO_LOCAL="rest:http://restic-rest-server-app.restic-rest:8000/gitea-database/" -p homelab -c home -t "$DOPPLER_TOKEN" > /dev/null 2>&1; echo "rc=$?"
```

- [ ] **Step 4: Add the Cloudflare tunnel service URL and AdGuard domain**

The exact in-cluster Service DNS name for `CF_TUNNEL_SVC_GITEA` depends on the
Service name the `gitea` chart creates — the chart's fullname template
produces `<release-name>-http` for the HTTP service (confirmed against
`service.http` in the chart's `values.yaml`; the ArgoCD Application's
`metadata.name`/Helm release name is `gitea-app`, so the Service is
`gitea-app-http`). Set this only after Task 6 confirms the release name —
placeholder-free because we fix the release name now and use it here:

```bash
cd /home/jonas/projects/infra-ops
source .env.doppler
doppler secrets set CF_TUNNEL_SVC_GITEA="http://gitea-app-http.gitea.svc.cluster.local:3000" -p infra-ops -c prd -t "$DOPPLER_TOKEN" > /dev/null 2>&1; echo "rc=$?"
doppler secrets set INFRA_UNIFI_DNS_GITEA_DOMAIN="git.janz.digital" -p infra-ops -c prd -t "$DOPPLER_TOKEN" > /dev/null 2>&1; echo "rc=$?"
```

- [ ] **Step 5: Verify all seven new keys exist (names only, never values)**

```bash
cd /home/jonas/projects/home-ops && source .env.doppler
doppler secrets get GITEA_ADMIN_PASSWORD GITEA_ADMIN_SSH_PUBLIC_KEY GITEA_POSTGRES_USER_PASSWORD GITEA_DATA_REST_REPO_LOCAL GITEA_DATABASE_REST_REPO_LOCAL --plain -p homelab -c home -t "$DOPPLER_TOKEN" | wc -l
# Expected: 5

cd /home/jonas/projects/infra-ops && source .env.doppler
doppler secrets get CF_TUNNEL_SVC_GITEA INFRA_UNIFI_DNS_GITEA_DOMAIN --plain -p infra-ops -c prd -t "$DOPPLER_TOKEN" | wc -l
# Expected: 2
```

No commit for this task (no files changed).

---

### Task 2: Cloudflare tunnel ingress + DNS record (infra-ops)

**Files:**
- Modify: `infra-ops/stacks/cloudflare/doppler.tf`
- Modify: `infra-ops/stacks/cloudflare/tunnels.tf`
- Modify: `infra-ops/stacks/cloudflare/dns.tf`

**Interfaces:**
- Consumes: `CF_TUNNEL_SVC_GITEA` (Task 1)
- Produces: public DNS record `git.janz.digital` → tunnel `k8s`, tunnel ingress rule routing to Gitea's Service.

- [ ] **Step 1: Add the `tunnel_svc_gitea` local**

In `doppler.tf`, after the `tunnel_svc_tymeslot` line:

```hcl
  tunnel_svc_gitea     = nonsensitive(data.doppler_secrets.this.map.CF_TUNNEL_SVC_GITEA)
```

- [ ] **Step 2: Add the tunnel ingress rule**

In `tunnels.tf`, inside the `k8s` tunnel's `cloudflare_zero_trust_tunnel_cloudflared_config` resource, insert before the trailing `{ service = "http_status:404" }` entry:

```hcl
      {
        hostname = "git.${local.zone_name}"
        service  = local.tunnel_svc_gitea
      },
```

- [ ] **Step 3: Add the DNS CNAME record**

In `dns.tf`, add to the `tunnel_cname_records` map (key `git`, matching the tunnel hostname `git.${local.zone_name}`):

```hcl
    git       = { record_id = null, tunnel_id = local.tunnel_k8s_id }
```

- [ ] **Step 4: Validate**

```bash
cd /home/jonas/projects/infra-ops
mise run terraform:init:cloudflare
mise run terraform:plan:cloudflare
```

Expected: plan shows exactly 2 additions (`cloudflare_zero_trust_tunnel_cloudflared_config.k8s` in-place update, `cloudflare_dns_record.tunnel["git"]` create) and 0 destructions. If anything else shows as changed/destroyed, stop — do not apply.

- [ ] **Step 5: Apply**

```bash
mise run terraform:apply:cloudflare -- -auto-approve
```

- [ ] **Step 6: Verify**

```bash
dig +short git.janz.digital @1.1.1.1
# Expected: a Cloudflare proxy IP (not the origin), since this is a proxied record
```

- [ ] **Step 7: Commit**

```bash
git add stacks/cloudflare/doppler.tf stacks/cloudflare/tunnels.tf stacks/cloudflare/dns.tf
git commit -m "$(cat <<'EOF'
feat(cloudflare): add git.janz.digital tunnel ingress for Gitea

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: AdGuard split-DNS rewrite (infra-ops)

**Files:**
- Modify: `infra-ops/mise.toml` (both `unifi-udm:check` and `unifi-udm:apply` tasks)
- Modify: `infra-ops/stacks/unifi/ansible/roles/udm_adguard/tasks/main.yml`

**Interfaces:**
- Consumes: `INFRA_UNIFI_DNS_GITEA_DOMAIN` (Task 1), existing `DNS_ALTUS_APPS_IP` (already exported by both mise tasks — reused, not new).
- Produces: AdGuard DNS rewrite `git.janz.digital` → the OpenShift router IP.

- [ ] **Step 1: Add the new env export to both mise tasks**

In `mise.toml`, in **both** `[tasks."unifi-udm:check"]` and `[tasks."unifi-udm:apply"]` bodies, immediately after the existing `DNS_ALTUS_APPS_IP=$(doppler secrets get INFRA_UNIFI_DNS_ALTUS_APPS_IP ...)` line:

```bash
DNS_GITEA_DOMAIN=$(doppler secrets get INFRA_UNIFI_DNS_GITEA_DOMAIN --project infra-ops --config prd --plain -t "$DOPPLER_TOKEN" --config-dir "$TMPDIR")
```

And add `DNS_GITEA_DOMAIN` to the existing `export DNS_ALTUS_APPS_DOMAIN DNS_ALTUS_APPS_IP ...` line in both tasks (append, don't replace the existing names):

```bash
export DNS_ALTUS_APPS_DOMAIN DNS_ALTUS_APPS_IP DNS_ALTUS_API_DOMAIN DNS_ALTUS_API_IP DNS_ALTUS_APPS2_DOMAIN DHCP_RESERVATIONS_JSON DNS_GITEA_DOMAIN
```

- [ ] **Step 2: Add the rewrite entry to the ansible role**

In `stacks/unifi/ansible/roles/udm_adguard/tasks/main.yml`, in the `agh_desired_rewrites` list (the task titled "Set desired AdGuard DNS rewrites"), add a new entry reusing `DNS_ALTUS_APPS_IP` as the answer (no new IP secret — Routes share one router IP):

```yaml
      - domain: "{{ lookup('env', 'DNS_GITEA_DOMAIN') }}"
        answer: "{{ lookup('env', 'DNS_ALTUS_APPS_IP') }}"
```

- [ ] **Step 3: Validate (check mode)**

```bash
cd /home/jonas/projects/infra-ops
mise run unifi-udm:check
```

Expected: diff shows one new AdGuard rewrite add for `git.janz.digital`, nothing else changed. (Per this repo's own documented gotcha, the "Verify AdGuardHome service is active" task can show empty stdout in check mode — that's expected, not a failure; judge success by the diff on the rewrite-add task only.)

- [ ] **Step 4: Apply**

```bash
mise run unifi-udm:apply
```

- [ ] **Step 5: Verify from a LAN-connected client**

```bash
# Run from a machine on the LAN, using AdGuard as resolver (not 1.1.1.1 — see repo's own dnsmasq/Tailscale DNS gotcha)
dig +short git.janz.digital
# Expected: the DNS_ALTUS_APPS_IP value (the OpenShift router IP), not a Cloudflare IP
```

- [ ] **Step 6: Commit**

```bash
git add mise.toml stacks/unifi/ansible/roles/udm_adguard/tasks/main.yml
git commit -m "$(cat <<'EOF'
feat(unifi): add AdGuard split-DNS rewrite for git.janz.digital

LAN clients resolve straight to the OpenShift router IP, bypassing
Cloudflare/cloudflared, matching the existing asgard/knowhere Tailscale
split-horizon pattern.

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Extend the wildcard certificate's SAN list (home-ops)

**Files:**
- Modify: `home-ops/components-infra/certificates/base/openshift-wildcard-certificate.yaml`

**Interfaces:**
- Produces: `git.janz.digital` as a valid SAN on the cluster's default IngressController certificate, so any Route for that hostname gets a real Let's Encrypt cert without a dedicated per-app `Certificate`.

- [ ] **Step 1: Add the SAN**

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: openshift-wildcard
  namespace: openshift-ingress
  annotations:
    argocd.argoproj.io/sync-wave: "0"
spec:
  secretName: openshift-wildcard-certificate
  issuerRef:
    name: cloudflare-issuer
    kind: ClusterIssuer
  commonName: "*.apps.altus.janz.digital"
  dnsNames:
    - "*.apps.altus.janz.digital"
    - "git.janz.digital"
```

(Only the `local.home`-targeting base file changes — the `overlays/sakaar/wildcard-certificate-patch.yaml` copy is untouched, since Gitea is Altus-only and that overlay only has `*.apps.sakaar.janz.cloud`.)

- [ ] **Step 2: Validate**

```bash
cd /home/jonas/projects/home-ops
kustomize build components-infra/certificates/base | grep -A5 "dnsNames"
# Expected: both *.apps.altus.janz.digital and git.janz.digital listed
```

- [ ] **Step 3: Commit (do not push yet — bundle with Task 10's final push, since this alone doesn't re-issue the cert until ArgoCD syncs)**

```bash
git add components-infra/certificates/base/openshift-wildcard-certificate.yaml
git commit -m "$(cat <<'EOF'
feat(certificates): add git.janz.digital SAN to default wildcard cert

Lets Gitea's LAN-direct Route present a real Let's Encrypt certificate
without a dedicated per-app Certificate resource.

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Gitea namespace, CNPG Postgres, ExternalSecrets (home-ops)

**Files:**
- Create: `home-ops/components-apps/gitea/namespace.yaml`
- Create: `home-ops/components-apps/gitea/postgresql-database.yaml`
- Create: `home-ops/components-apps/gitea/external-gitea-db-secret.yaml`
- Create: `home-ops/components-apps/gitea/external-gitea-admin-secret.yaml`
- Create: `home-ops/components-apps/gitea/kustomization.yaml` (grows in later tasks — start it here)

**Interfaces:**
- Consumes: `GITEA_POSTGRES_USER_PASSWORD`, `GITEA_ADMIN_PASSWORD`, `GITEA_ADMIN_SSH_PUBLIC_KEY` (Task 1)
- Produces: Secret `gitea-db-secret` (keys `username`, `password`) for Task 6's DB env wiring; Secret `gitea-admin-secret` (keys `username`, `password`, `email`) for Task 6's `gitea.admin.existingSecret`; CNPG `Cluster` `gitea-db`.

- [ ] **Step 1: Namespace**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "0"
  name: gitea
```

- [ ] **Step 2: CNPG Postgres cluster**

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: gitea-db
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "100"
spec:
  instances: 1
  enablePDB: false

  bootstrap:
    initdb:
      database: gitea
      owner: gitea
      secret:
        name: gitea-db-secret

  storage:
    size: 10Gi
```

- [ ] **Step 3: DB ExternalSecret**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: gitea-db-secret
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: gitea-db-secret
    template:
      engineVersion: v2
      type: kubernetes.io/basic-auth
      data:
        username: "gitea"
        password: "{{ .password }}"
  data:
    - secretKey: password
      remoteRef:
        key: GITEA_POSTGRES_USER_PASSWORD
```

- [ ] **Step 4: Admin bootstrap ExternalSecret**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: gitea-admin-secret
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: gitea-admin-secret
    template:
      engineVersion: v2
      data:
        username: "jonas"
        password: "{{ .password }}"
        email: "jonas@famjanz.de"
  data:
    - secretKey: password
      remoteRef:
        key: GITEA_ADMIN_PASSWORD
```

- [ ] **Step 5: Start the kustomization**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - postgresql-database.yaml
  - external-gitea-db-secret.yaml
  - external-gitea-admin-secret.yaml
```

- [ ] **Step 6: Validate**

```bash
cd /home/jonas/projects/home-ops
kustomize build components-apps/gitea
```

Expected: renders 4 objects (Namespace, Cluster, 2 ExternalSecrets) with no errors.

- [ ] **Step 7: Commit**

```bash
git add components-apps/gitea/namespace.yaml components-apps/gitea/postgresql-database.yaml components-apps/gitea/external-gitea-db-secret.yaml components-apps/gitea/external-gitea-admin-secret.yaml components-apps/gitea/kustomization.yaml
git commit -m "$(cat <<'EOF'
feat(gitea): add namespace, CNPG Postgres, and bootstrap secrets

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Gitea Helm chart Application (home-ops)

**Files:**
- Create: `home-ops/components-apps/gitea/gitea-chart-app.yaml`
- Modify: `home-ops/components-apps/gitea/kustomization.yaml` (add the new file)

**Interfaces:**
- Consumes: `gitea-db-secret` (Task 5, keys `username`/`password`), `gitea-admin-secret` (Task 5, keys `username`/`password`/`email`)
- Produces: ArgoCD Application `gitea-app`, which creates Service `gitea-app-http` (consumed by Task 2's `CF_TUNNEL_SVC_GITEA` value — already fixed to this name) and `gitea-app-ssh`, Route `git.janz.digital`, PVC `gitea-app` (chart default persistence claim name pattern — verify via `oc get pvc -n gitea` after first sync, per this repo's own documented "don't guess PVC names" rule, before Task 7 writes the backup's `sourcePVC`).

- [ ] **Step 1: Write the Application**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: gitea-app
  namespace: openshift-gitops
  annotations:
    argocd.argoproj.io/sync-wave: "111"
spec:
  destination:
    namespace: gitea
    server: "https://kubernetes.default.svc"
  source:
    chart: gitea
    repoURL: https://dl.gitea.com/charts
    targetRevision: 1.5.5
    helm:
      values: |-
        image:
          rootless: true

        gitea:
          admin:
            existingSecret: gitea-admin-secret
          config:
            server:
              ROOT_URL: "https://git.janz.digital/"
              DOMAIN: "git.janz.digital"
            service:
              DISABLE_REGISTRATION: true
            security:
              REVERSE_PROXY_TRUSTED_PROXIES: "10.0.0.0/8"
            actions:
              ENABLED: true

        deployment:
          env:
            - name: GITEA__database__DB_TYPE
              value: postgres
            - name: GITEA__database__HOST
              value: "gitea-db-rw:5432"
            - name: GITEA__database__NAME
              value: gitea
            - name: GITEA__database__USER
              value: gitea
            - name: GITEA__database__PASSWD
              valueFrom:
                secretKeyRef:
                  name: gitea-db-secret
                  key: password

        postgresql-ha:
          enabled: false
        postgresql:
          enabled: false
        valkey:
          enabled: false

        service:
          ssh:
            annotations:
              tailscale.com/expose: "true"
              tailscale.com/hostname: "gitea"

        route:
          enabled: true
          host: git.janz.digital
          wildcardPolicy: None
          tls:
            termination: edge

        persistence:
          enabled: true
          size: 20Gi
          storageClass: lvms-vg1

  project: cluster-apps
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
    automated:
      prune: true
      selfHeal: true
```

Note on `REVERSE_PROXY_TRUSTED_PROXIES`: `10.0.0.0/8` is a placeholder-free
but provisional value — Task 6's Step 4 verification must confirm this
actually covers the cloudflared pod's and router's real source IPs on
Altus (check via `oc get pods -n cloudflared -o wide` and the router
pod's IP range) and narrow it if `10.0.0.0/8` is broader than the
cluster's actual pod CIDR.

- [ ] **Step 2: Add to kustomization.yaml**

```yaml
resources:
  - namespace.yaml
  - postgresql-database.yaml
  - external-gitea-db-secret.yaml
  - external-gitea-admin-secret.yaml
  - gitea-chart-app.yaml
```

- [ ] **Step 3: Validate**

```bash
cd /home/jonas/projects/home-ops
kustomize build components-apps/gitea | grep -c "^kind: Application"
# Expected: 1 (this task's Application; Task 9 adds a second)
helm template gitea-app https://dl.gitea.com/charts/gitea-1.5.5.tgz -f <(kustomize build components-apps/gitea | yq 'select(.metadata.name == "gitea-app") | .spec.source.helm.values' -r) > /dev/null
echo "helm template rc=$?"
```

- [ ] **Step 4: Commit**

```bash
git add components-apps/gitea/gitea-chart-app.yaml components-apps/gitea/kustomization.yaml
git commit -m "$(cat <<'EOF'
feat(gitea): add Gitea Helm chart Application

Route on git.janz.digital (LAN-direct, rides the extended wildcard cert),
public path via the existing cloudflared tunnel to gitea-app-http, SSH
exposed to the tailnet via the Tailscale operator, external CNPG Postgres.

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

(Do not push yet — Task 10 does the coordinated first push once `values-apps.yaml` registers the app.)

---

### Task 7: Backups — local restic only (home-ops)

**Files:**
- Create: `home-ops/components-apps/gitea/backup/kustomization.yaml`
- Create: `home-ops/components-apps/gitea/backup/data/kustomization.yaml`
- Create: `home-ops/components-apps/gitea/backup/data/local.yaml`
- Create: `home-ops/components-apps/gitea/backup/data/local-secret.yaml`
- Create: `home-ops/components-apps/gitea/backup/database/kustomization.yaml`
- Create: `home-ops/components-apps/gitea/backup/database/local.yaml`
- Create: `home-ops/components-apps/gitea/backup/database/local-secret.yaml`
- Create: `home-ops/components-apps/gitea/backup/database/cron.yaml`
- Create: `home-ops/components-apps/gitea/backup/database/rolebinding.yaml`
- Create: `home-ops/components-apps/gitea/backup/database/pvc.yaml` (namespace override — the shared `templates/psql-backup/pvc.yaml` hardcodes `namespace: paperless`)
- Modify: `home-ops/components-apps/gitea/kustomization.yaml`

**Interfaces:**
- Consumes: real PVC name for `/data` from `oc get pvc -n gitea` (confirm before writing `local.yaml` — do not guess), `GITEA_DATA_REST_REPO_LOCAL`/`GITEA_DATABASE_REST_REPO_LOCAL` (Task 1), `gitea-db-secret` (Task 5).

- [ ] **Step 1: Confirm the real PVC name (requires Task 6 already synced to the cluster — do this task after Task 6's changes have been pushed and ArgoCD has created the PVC, not before)**

```bash
oc get pvc -n gitea
```

Use the exact name shown for the chart's data volume in `backup/data/local.yaml` below — do not assume a name.

- [ ] **Step 2: `backup/kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ./data
  - ./database
```

- [ ] **Step 3: `backup/data/kustomization.yaml`** (local restic only — no `templates/volsync/backblaze`)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namePrefix: data-
namespace: gitea

resources:
  - ../../../../templates/volsync/rest

patches:
  - target:
      group: volsync.backube
      version: v1alpha1
      kind: ReplicationSource
      name: backup
    path: local.yaml
  - target:
      group: external-secrets.io
      version: v1
      kind: ExternalSecret
      name: external-restic-config
    path: local-secret.yaml
```

- [ ] **Step 4: `backup/data/local.yaml`** (replace `<real-pvc-name>` with the value from Step 1)

```yaml
- op: replace
  path: /spec/sourcePVC
  value: <real-pvc-name>
- op: replace
  path: /spec/restic/repository
  value: restic-config-data
```

- [ ] **Step 5: `backup/data/local-secret.yaml`**

```yaml
- op: replace
  path: /spec/target/name
  value: restic-config-data
- op: replace
  path: /spec/data/0/remoteRef/key
  value: GITEA_DATA_REST_REPO_LOCAL
```

- [ ] **Step 6: `backup/database/kustomization.yaml`** (mirrors `paperless/backup/database`, local-only)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namePrefix: database-
namespace: gitea

resources:
  - ../../../../templates/psql-backup
  - ../../../../templates/volsync/rest

patches:
  - target:
      group: volsync.backube
      version: v1alpha1
      kind: ReplicationSource
      name: backup
    path: local.yaml
  - target:
      group: external-secrets.io
      version: v1
      kind: ExternalSecret
      name: external-restic-config
    path: local-secret.yaml
  - target:
      group: batch
      version: v1
      kind: CronJob
      name: psql-backup
    path: cron.yaml
  - target:
      group: rbac.authorization.k8s.io
      version: v1
      kind: RoleBinding
      name: psql-backup
    path: rolebinding.yaml
  - target:
      group: ""
      version: v1
      kind: PersistentVolumeClaim
      name: syno-db-backup
    path: pvc.yaml
```

- [ ] **Step 7: `backup/database/local.yaml`**

```yaml
- op: replace
  path: /spec/sourcePVC
  value: database-syno-db-backup
- op: replace
  path: /spec/restic/repository
  value: restic-config-database
```

- [ ] **Step 8: `backup/database/local-secret.yaml`**

```yaml
- op: replace
  path: /spec/target/name
  value: restic-config-database
- op: replace
  path: /spec/data/0/remoteRef/key
  value: GITEA_DATABASE_REST_REPO_LOCAL
```

- [ ] **Step 9: `backup/database/cron.yaml`**

```yaml
- op: replace
  path: /spec/jobTemplate/spec/template/spec/volumes/0/persistentVolumeClaim/claimName
  value: database-syno-db-backup

- op: replace
  path: /spec/jobTemplate/spec/template/spec/containers/0/env
  value:
    - name: POSTGRES_USER
      value: gitea
    - name: POSTGRES_POSTGRES_PASSWORD
      valueFrom:
        secretKeyRef:
          name: gitea-db-secret
          key: password
```

- [ ] **Step 10: `backup/database/rolebinding.yaml`**

```yaml
- op: replace
  path: /subjects
  value:
    - kind: ServiceAccount
      name: database-psql-backup
      namespace: gitea
```

- [ ] **Step 11: `backup/database/pvc.yaml`** (the shared template hardcodes `namespace: paperless` — override it)

```yaml
- op: replace
  path: /metadata/namespace
  value: gitea
```

- [ ] **Step 12: Add `./backup` to the app's kustomization.yaml**

```yaml
resources:
  - namespace.yaml
  - postgresql-database.yaml
  - external-gitea-db-secret.yaml
  - external-gitea-admin-secret.yaml
  - gitea-chart-app.yaml
  - ./backup
```

- [ ] **Step 13: Validate**

```bash
cd /home/jonas/projects/home-ops
kustomize build components-apps/gitea | grep -c "kind: ReplicationSource"
# Expected: 2 (data-backup, database-backup)
kustomize build components-apps/gitea | grep "repository:"
# Expected: only restic-config-data / restic-config-database — no *-b2 entries anywhere
```

- [ ] **Step 14: Commit**

```bash
git add components-apps/gitea/backup
git add components-apps/gitea/kustomization.yaml
git commit -m "$(cat <<'EOF'
feat(gitea): add local-restic-only backups for data and database

No Backblaze/offsite copy, per explicit instruction — on-site only.

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: SSH key + Actions registration-token bootstrap Job (home-ops)

**Files:**
- Create: `home-ops/components-apps/gitea/bootstrap-job.yaml`
- Create: `home-ops/components-apps/gitea/bootstrap-rbac.yaml`
- Modify: `home-ops/components-apps/gitea/kustomization.yaml`

**Interfaces:**
- Consumes: `gitea-admin-secret` (Task 5, `username`/`password`), `gitea-app-http` Service (Task 6)
- Produces: Secret `gitea-actions-runner-token` (key `token`) consumed by Task 9's `existingSecret`/`existingSecretKey`.

- [ ] **Step 1: RBAC — the Job's ServiceAccount needs to create/patch a Secret in its own namespace**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gitea-bootstrap
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: gitea-bootstrap-secrets
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: gitea-bootstrap-secrets
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
subjects:
  - kind: ServiceAccount
    name: gitea-bootstrap
    namespace: gitea
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: gitea-bootstrap-secrets
```

- [ ] **Step 2: The bootstrap Job itself, as an ArgoCD PostSync hook**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: gitea-bootstrap
  namespace: gitea
  annotations:
    argocd.argoproj.io/hook: PostSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
spec:
  backoffLimit: 3
  template:
    spec:
      serviceAccountName: gitea-bootstrap
      restartPolicy: Never
      containers:
        - name: bootstrap
          image: quay.io/openshift/origin-cli:4.22
          env:
            - name: GITEA_URL
              value: "http://gitea-app-http.gitea.svc.cluster.local:3000"
            - name: GITEA_ADMIN_USER
              valueFrom:
                secretKeyRef:
                  name: gitea-admin-secret
                  key: username
            - name: GITEA_ADMIN_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: gitea-admin-secret
                  key: password
            - name: SSH_PUBLIC_KEY
              valueFrom:
                secretKeyRef:
                  name: gitea-ssh-key
                  key: public-key
          command: ["/bin/bash", "-c"]
          args:
            - |-
              set -euo pipefail

              echo "Waiting for Gitea to become reachable..."
              until curl -sf -o /dev/null "$GITEA_URL/api/v1/version"; do
                sleep 5
              done
              echo "Gitea is up."

              echo "Authorizing SSH key..."
              curl -sf -u "$GITEA_ADMIN_USER:$GITEA_ADMIN_PASSWORD" \
                -X POST "$GITEA_URL/api/v1/user/keys" \
                -H "Content-Type: application/json" \
                -d "{\"title\":\"jonas (personal)\",\"key\":\"$SSH_PUBLIC_KEY\"}" \
                || echo "SSH key add returned non-zero (likely already exists) - continuing"

              echo "Requesting Actions runner registration token..."
              TOKEN=$(curl -sf -u "$GITEA_ADMIN_USER:$GITEA_ADMIN_PASSWORD" \
                -X POST "$GITEA_URL/api/v1/admin/actions/runners/registration-token" \
                | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

              if [ -z "$TOKEN" ]; then
                echo "ERROR: empty registration token" >&2
                exit 1
              fi

              echo "Writing token to gitea-actions-runner-token Secret..."
              oc create secret generic gitea-actions-runner-token \
                -n gitea --from-literal=token="$TOKEN" \
                --dry-run=client -o yaml | oc apply -f -

              echo "Bootstrap complete."
```

- [ ] **Step 3: The SSH public key needs its own Secret (ExternalSecret), referenced above**

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: gitea-ssh-key
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: gitea-ssh-key
  data:
    - secretKey: public-key
      remoteRef:
        key: GITEA_ADMIN_SSH_PUBLIC_KEY
```

Add this as a new file `external-gitea-ssh-key.yaml` in the same directory.

- [ ] **Step 4: Add both new files to kustomization.yaml**

```yaml
resources:
  - namespace.yaml
  - postgresql-database.yaml
  - external-gitea-db-secret.yaml
  - external-gitea-admin-secret.yaml
  - external-gitea-ssh-key.yaml
  - gitea-chart-app.yaml
  - ./backup
  - bootstrap-rbac.yaml
  - bootstrap-job.yaml
```

- [ ] **Step 5: Validate**

```bash
cd /home/jonas/projects/home-ops
kustomize build components-apps/gitea > /dev/null
echo "rc=$?"
kustomize build components-apps/gitea | grep -A2 "kind: Job"
```

- [ ] **Step 6: Commit**

```bash
git add components-apps/gitea/bootstrap-job.yaml components-apps/gitea/bootstrap-rbac.yaml components-apps/gitea/external-gitea-ssh-key.yaml components-apps/gitea/kustomization.yaml
git commit -m "$(cat <<'EOF'
feat(gitea): add PostSync bootstrap Job for SSH key + Actions token

Authorizes the admin's SSH key via the Gitea API and mints the instance-
level Actions runner registration token, writing it to a Secret the
actions chart Application (next task) consumes.

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Actions runner — privileged SCC + actions chart Application (home-ops)

**Files:**
- Create: `home-ops/components-apps/gitea/gitea-actions-privileged-clusterrolebinding.yaml`
- Create: `home-ops/components-apps/gitea/gitea-actions-chart-app.yaml`
- Modify: `home-ops/components-apps/gitea/kustomization.yaml`

**Interfaces:**
- Consumes: Secret `gitea-actions-runner-token` (Task 8, key `token`)
- Produces: a running, registered Gitea Actions runner with Docker execution (DinD sidecar).

- [ ] **Step 1: ServiceAccount + privileged ClusterRoleBinding**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gitea-actions-runner
  namespace: gitea
  annotations:
    argocd.argoproj.io/sync-wave: "30"
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: gitea-actions-runner-privileged
  annotations:
    argocd.argoproj.io/sync-wave: "30"
subjects:
  - kind: ServiceAccount
    name: gitea-actions-runner
    namespace: gitea
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:openshift:scc:privileged
```

- [ ] **Step 2: The `actions` chart Application**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: gitea-actions-app
  namespace: openshift-gitops
  annotations:
    argocd.argoproj.io/sync-wave: "112"
spec:
  destination:
    namespace: gitea
    server: "https://kubernetes.default.svc"
  source:
    chart: actions
    repoURL: https://dl.gitea.com/charts
    targetRevision: 0.1.2
    helm:
      values: |-
        enabled: true
        giteaRootURL: "http://gitea-app-http.gitea.svc.cluster.local:3000"
        existingSecret: "gitea-actions-runner-token"
        existingSecretKey: "token"
        statefulset:
          serviceAccountName: gitea-actions-runner
          persistence:
            size: 5Gi

  project: cluster-apps
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
    automated:
      prune: true
      selfHeal: true
```

- [ ] **Step 3: Add both new files to kustomization.yaml**

```yaml
resources:
  - namespace.yaml
  - postgresql-database.yaml
  - external-gitea-db-secret.yaml
  - external-gitea-admin-secret.yaml
  - external-gitea-ssh-key.yaml
  - gitea-chart-app.yaml
  - ./backup
  - bootstrap-rbac.yaml
  - bootstrap-job.yaml
  - gitea-actions-privileged-clusterrolebinding.yaml
  - gitea-actions-chart-app.yaml
```

- [ ] **Step 4: Validate**

```bash
cd /home/jonas/projects/home-ops
kustomize build components-apps/gitea | grep -c "^kind: Application"
# Expected: 2 (gitea-app, gitea-actions-app)
```

- [ ] **Step 5: Commit**

```bash
git add components-apps/gitea/gitea-actions-privileged-clusterrolebinding.yaml components-apps/gitea/gitea-actions-chart-app.yaml components-apps/gitea/kustomization.yaml
git commit -m "$(cat <<'EOF'
feat(gitea): add Gitea Actions runner with Docker execution

Uses the official gitea/helm-actions chart (privileged DinD sidecar,
hardcoded in its StatefulSet template) rather than porting this repo's
GitHub-runner podman-shim image — that pattern solved a problem (no
ready-made runner chart) that doesn't exist here. Only the privileged-SCC
grant carries over, applied to a dedicated gitea-actions-runner SA.

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Register in values-apps.yaml, push, and end-to-end verification

**Files:**
- Modify: `home-ops/bootstrap/overlays/local.home/values-apps.yaml`

**Interfaces:** none new — this task wires everything from Tasks 4-9 into ArgoCD and verifies the live result.

- [ ] **Step 1: Register the app (Altus/`local.home` only — no `sakaar` entry)**

```yaml
  gitea:
    annotations:
      argocd.argoproj.io/sync-wave: "300"
    source:
      path: components-apps/gitea
```

- [ ] **Step 2: Validate the whole overlay still builds**

```bash
cd /home/jonas/projects/home-ops
kustomize build bootstrap/overlays/local.home > /dev/null
echo "rc=$?"
```

- [ ] **Step 3: Commit**

```bash
git add bootstrap/overlays/local.home/values-apps.yaml
git commit -m "$(cat <<'EOF'
feat(gitea): register gitea app on Altus (local.home)

Assisted-by: Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Push everything (Tasks 4-10) — this is the point ArgoCD starts acting**

```bash
git push origin main
```

- [ ] **Step 5: Watch the sync**

```bash
watch -n5 'oc get application -n openshift-gitops gitea gitea-app gitea-actions-app -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
```

Wait until all three show `Synced` / `Healthy` (or `Progressing` settling to `Healthy` — CNPG bootstrap and the chart's own init containers take a few minutes).

- [ ] **Step 6: Confirm the real `/data` PVC name if Task 7 was done before this point**

If Task 7 was executed strictly in order (after Task 6 alone was live), its PVC name should already be confirmed. If the whole plan was written and pushed in one batch, run this now and fix `backup/data/local.yaml` if the guessed name doesn't match:

```bash
oc get pvc -n gitea
```

- [ ] **Step 7: Verify the public path**

```bash
curl -sI https://git.janz.digital/api/v1/version | head -1
# Expected: HTTP/2 200
```

- [ ] **Step 8: Verify the LAN-direct path presents a real Let's Encrypt cert**

Run from a LAN-connected machine:

```bash
echo | openssl s_client -connect git.janz.digital:443 -servername git.janz.digital 2>/dev/null | openssl x509 -noout -issuer
# Expected: issuer containing "Let's Encrypt"
```

- [ ] **Step 9: Verify SSH access over Tailscale**

```bash
ssh -T git@gitea.<tailnet>.ts.net
# Expected: "Hi jonas! You've successfully authenticated..." (replace <tailnet> with the real tailnet domain, from TAILSCALE_TAILNET_DOMAIN)
```

- [ ] **Step 10: Verify the Actions runner registered**

```bash
curl -sf -u "jonas:<admin-password-from-Doppler>" https://git.janz.digital/api/v1/admin/actions/runners | python3 -m json.tool
# Expected: one runner listed, status "online"
```

(Fetch the admin password via the CLI-suppressed pattern into a shell variable, never print it, when running this check.)

- [ ] **Step 11: Confirm backups ran**

```bash
oc get replicationsource -n gitea
# Expected: data-backup and database-backup both show lastSyncTime populated after their first scheduled trigger, or force one:
oc annotate replicationsource -n gitea data-backup volsync.backube/target-trigger="$(date +%s)" --overwrite
```

- [ ] **Step 12: Final report**

No code changes in this step — summarize live state (all Applications healthy, all three network paths verified, Actions runner online, backups confirmed) back to the user.

---

## Self-Review Notes

- **Spec coverage:** all sections of the design spec map to a task — hostnames/paths (Tasks 2-4, 6), deployment (Tasks 5-6), account bootstrap (Tasks 1, 8), baseline config (Task 6), Actions (Tasks 8-9), backup (Task 7), Terraform/Ansible (Tasks 2-3), values-apps.yaml registration (Task 10).
- **Known soft spots flagged inline, not hidden:** the exact `REVERSE_PROXY_TRUSTED_PROXIES` CIDR (Task 6) and the exact `/data` PVC name (Tasks 7 and 10 Step 6) both depend on live-cluster values that can't be known until the chart's first sync — both have explicit verify-and-fix steps rather than being asserted as fact.
- **Ordering dependency:** Task 7 (backups) needs the real PVC name, which only exists after Task 6 is live. Tasks 2-9 can be committed in any order, but the actual `git push` in Task 10 Step 4 is the first point anything goes live — if execution pushes earlier (e.g. per-task pushes instead of one batch at the end), do Task 7's PVC-name confirmation immediately after Task 6's push lands, not from a guess.
