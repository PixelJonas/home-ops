# Adding a New App

This repo is multi-cluster: `bootstrap/overlays/local.home/` (Altus, bare-metal
on-prem SNO) and `bootstrap/overlays/sakaar/` (Sakaar, Hetzner-hosted SNO).
Most apps run on both; some are cluster-specific. Register and, where
necessary, overlay every new app for **both** clusters — don't build it for
one and forget the other.

## File Structure

Every app under `components-apps/<app-name>/` follows this layout:

```
components-apps/<app>/
├── kustomization.yaml          # lists all base resources + ./backup
├── namespace.yaml              # Namespace, sync-wave 0
├── anyuid-rolebinding.yaml     # Only if the container needs a specific UID (see Pod Security below)
├── <app>-chart-app.yaml        # ArgoCD Application pointing at bjw-s app-template
├── backup/
│   ├── kustomization.yaml      # lists sub-directories
│   └── data/                   # one sub-directory per volume to back up
│       ├── kustomization.yaml
│       ├── local.yaml
│       ├── local-secret.yaml
│       ├── backblaze.yaml
│       └── backblaze-secret.yaml
└── overlays/sakaar/             # only if anything differs between clusters
    ├── kustomization.yaml
    ├── namespace.yaml           # copy, not a ../../ reference (see below)
    └── <app>-chart-app.yaml     # copy, with the cluster-specific fields patched
```

After creating the base files, register the app in **both**
`bootstrap/overlays/local.home/values-apps.yaml` and
`bootstrap/overlays/sakaar/values-apps.yaml`:

```yaml
  <app-name>:
    annotations:
      argocd.argoproj.io/sync-wave: "300"
    source:
      path: components-apps/<app-name>              # local.home: base directly
```

```yaml
  <app-name>:
    annotations:
      argocd.argoproj.io/sync-wave: "300"
    source:
      path: components-apps/<app-name>/overlays/sakaar   # sakaar: overlay if one exists, else base
```

If nothing differs between clusters (no hostname/storage-class/secret-store
change needed), both entries can point at the same base path and no
`overlays/sakaar/` directory is needed at all — e.g. `tax-agent` has no
ingress and no cluster-specific values, so it's registered identically on
both. If anything *does* differ (almost always: the ingress hostname), build
the overlay.

### The overlay-copy pattern (and why it's not `../../`)

`overlays/sakaar/kustomization.yaml` lists the files it needs **copied
locally**, not referenced via `../../` back into the base directory:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - external-<app>-credentials.yaml
  - <app>-chart-app.yaml
  - ../../backup       # shared, unchanged resources CAN be referenced normally
```

Kustomize rejects individual-file references to a parent directory (`../../
namespace.yaml`) and detects a cycle when an overlay nested *inside* a base
kustomization directory references that base kustomization directly. The
workaround is: copy any file that needs a cluster-specific patch into the
overlay directory, keep only genuinely-shared, unpatched resources (like
`../../backup`) as real references. **This means a copied file can silently
drift from its base counterpart** — when editing a value that should be
identical on both clusters (e.g. an env var unrelated to cluster identity),
check both copies, not just the one you're looking at. See
`components-apps/silverbullet/` for a complete working example of this
pattern.

---

## Namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "0"
  name: <app-name>
```

---

## ArgoCD Application (bjw-s app-template)

Always use chart `app-template` from `https://bjw-s-labs.github.io/helm-charts`, current version **4.6.2**.
The Application must live in namespace `openshift-gitops` and project `cluster-apps`.

**Naming:** the values-apps.yaml entry produces a mid-tier ArgoCD Application
named after the key (e.g. `mealie`). The inner Helm chart Application it
generates **must use a different name** to avoid conflicts — convention is
`<app-name>-app` (e.g. `mealie-app`). This also affects PVC names: a
persistence key `data` on app `mealie-app` produces PVC `mealie-app-data`.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: <app-name>-app
  namespace: openshift-gitops
  annotations:
    argocd.argoproj.io/sync-wave: "111"
spec:
  destination:
    namespace: <app-name>
    server: "https://kubernetes.default.svc"
  source:
    chart: app-template
    repoURL: https://bjw-s-labs.github.io/helm-charts
    targetRevision: 4.6.2
    helm:
      values: |-
        controllers:
          <app-name>:
            containers:
              <app-name>:
                image:
                  repository: <registry>/<image>
                  tag: <version>
                  pullPolicy: IfNotPresent
                env:
                  TZ: Europe/Berlin

        service:
          <app-name>:
            controller: <app-name>
            ports:
              http:
                port: <port>

        ingress:
          <app-name>:
            enabled: true
            className: openshift-default
            annotations:
              route.openshift.io/termination: "edge"
            hosts:
              - host: <app-name>.apps.altus.janz.digital    # sakaar overlay copy uses <app-name>.apps.sakaar.janz.cloud
                paths:
                  - path: /
                    service:
                      identifier: <app-name>
                      port: http

        persistence:
          data:
            enabled: true
            globalMounts:
              - path: /data
            accessMode: ReadWriteOnce
            storageClass: lvms-vg1   # same class name on both clusters (each has its own LVM Operator vg1 device class)
            size: 5Gi

  project: cluster-apps
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
    automated:
      prune: true
      selfHeal: true
```

The PVC created for a persistence key `data` on an app named `<app-name>-app`
will be named `<app-name>-app-data`. This name is what the volsync backup
patches reference as `sourcePVC`.

**Storage class choice:**
- `lvms-vg1` — local LVM storage, same class name on both clusters, default
  for anything that doesn't need to be shared/large.
- `synology-nfs-storage` — dynamically-provisioned NFS from the Synology CSI
  driver, for larger or multi-pod-shared volumes. Only wired into
  `local.home` (Altus) today — there is no Sakaar equivalent yet, since
  reaching the NAS from Sakaar means routing over Tailscale (works, but is a
  deliberate per-app decision, not a default — see how `immich`'s Sakaar
  overlay switches to a static PV/PVC pointing at the NAS's Tailscale IP
  instead of dynamic provisioning).
- `nfs-custom` — **not** a real dynamic provisioner (`provisioner: custom`
  never actually provisions anything). It's a marker class used for
  statically-bound PV/PVC pairs where you hand-write the `PersistentVolume`
  yourself (NFS server/path already known) and just need a `storageClassName`
  that both sides agree on to bind. See `components-apps/paperless/
  paperless-consume-pvc.yaml` for the pattern.

---

## OpenShift-Specific Considerations

### Ingress
Always use `className: openshift-default` with the annotation `route.openshift.io/termination: "edge"`.

### Sync waves inside a component
| Resource | Wave |
|----------|------|
| Namespace | 0 |
| RBAC / ExternalSecrets | 30 |
| Databases (CloudNativePG) | 100 |
| Helm chart Application | 111 |
| Backup / CronJobs | 200 |

### Modifying a live app safely: the self-heal gotcha

Every leaf Application should have `syncPolicy.automated: {selfHeal: true,
prune: true}` (the repo-wide standard — see `bootstrap/overlays/*/
values-{apps,infra}.yaml`'s `autoSyncPrune: true`). This is good for normal
operation but means a **manual, in-place edit to a live resource** (e.g.
`oc scale` or `oc patch` while debugging) gets reverted almost immediately —
by the leaf Application's own selfHeal, or, if you tried to disable
`automated` on just the leaf, by the **mid-tier** Application's reconciliation
regenerating the leaf's spec including its `syncPolicy`. If you need a
resource to actually stay in a manually-changed state (e.g. scaled to 0
during a data migration), disable `automated` on **both** the mid-tier
Application (the `values-apps.yaml` key's own Application, e.g. `mealie`)
**and** the leaf (`mealie-app`) before touching anything — then re-enable
both afterward. Forgetting the mid-tier level is the most common way this
silently fails.

---

## Pod Security (OpenShift SCCs)

By default OpenShift assigns a random UID from the namespace range (`restricted-v2` SCC). Most images expect to run as a specific UID and will fail to write files if the UID doesn't match.

### Image runs as a specific non-root UID (e.g. 911)
Add an `anyuid` ClusterRoleBinding and nothing else:

```yaml
kind: ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: <app-name>-anyuid
  annotations:
    argocd.argoproj.io/sync-wave: "30"
subjects:
  - kind: ServiceAccount
    name: default
    namespace: <app-name>
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: "system:openshift:scc:anyuid"
```

### Image runs as root (UID 0)
Same `anyuid` ClusterRoleBinding as above, plus an explicit securityContext in the helm values to make it unambiguous:

```yaml
defaultPodOptions:
  securityContext:
    runAsUser: 0
    runAsGroup: 0
```

`anyuid` covers root — `privileged` SCC is **not** needed unless the container also requires host-level capabilities (host networking, `SYS_ADMIN`, etc.).

### Image works with any UID
No rolebinding needed. Omit the anyuid file and remove it from `kustomization.yaml`.

---

## Alpine DNS Issue on Kubernetes

Alpine-based images use `musl libc` which does not honour the standard Kubernetes DNS search path correctly, causing service name lookups to fail silently.

**Fix:** override `dnsPolicy` and supply the cluster DNS server explicitly in `defaultPodOptions`:

```yaml
defaultPodOptions:
  dnsPolicy: None
  dnsConfig:
    nameservers:
      - 172.30.0.10        # OpenShift cluster DNS service IP
    searches:
      - svc.cluster.local
      - cluster.local
      - altus.janz.digital     # local.home only — sakaar overlay drops this and uses sakaar.janz.cloud instead
      - janz.lan               # local.home only
      - janz.digital
```

Don't copy a `dnsConfig` block verbatim from an app's base file into its
sakaar overlay copy without updating the cluster-specific search domains —
`firefly`'s sakaar overlay shipped with stale `altus.janz.digital`/`janz.lan`
entries for exactly this reason (harmless — just extra failed resolver
attempts — but a sign the copy wasn't reviewed against its base after
copying).

Examples in this repo: [n8n.yaml](../components-apps/n8n/n8n.yaml).

---

## Secrets: which ClusterSecretStore to use

Two `ClusterSecretStore` objects exist, backed by different Doppler
projects:

| Store | Doppler project | Exists on |
|-------|------------------|-----------|
| `doppler-cluster` | `homelab/home` (app secrets — DB passwords, API keys, per-app config) | both clusters |
| `doppler-infra` | `infra-ops/prd` (cluster-infrastructure secrets — certs, oauth htpasswd) | **Sakaar only** |

**Almost every app `ExternalSecret` should use `doppler-cluster`** — it's the
only one that exists on both clusters, so an app built against it works
unmodified on either. Only reach for `doppler-infra` for a component that is
genuinely Sakaar-specific infrastructure (see
`components-infra/openshift-oauth-htpasswd/overlays/sakaar/`) — if you find
yourself wanting `doppler-infra` for something that should also run on
Altus, that's a sign the secret store choice (or the app's cluster scope) is
wrong, not that `doppler-infra` needs porting to `local.home`.

---

## Backup (volsync)

Each volume that needs backup gets its own sub-directory under `backup/`.
The pattern uses volsync `ReplicationSource` templates from `templates/` —
**three** template kinds, composed per-app as needed:

- `templates/volsync/rest` — backup to the in-cluster restic REST server
  (NFS-backed on the Synology NAS). Every app should have this.
- `templates/volsync/backblaze` — offsite backup to Backblaze B2. Add for
  anything you'd actually want to restore after a real disaster, not just
  local drive failure.
- `templates/psql-backup` — for CNPG/Postgres apps, a `pg_dump` CronJob
  writing to a PVC, composed alongside (not instead of) the volsync
  templates above.

(There is no MinIO-backed template anymore — `templates/volsync/base` was
retired; every app uses the REST server template.)

### `backup/<volume>/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namePrefix: <volume>-
namespace: <app-name>

resources:
  - ../../../../templates/volsync/rest        # in-cluster restic REST server
  - ../../../../templates/volsync/backblaze   # Backblaze B2 (offsite)

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
      group: volsync.backube
      version: v1alpha1
      kind: ReplicationSource
      name: backup-b2
    path: backblaze.yaml
  - target:
      group: external-secrets.io
      version: v1
      kind: ExternalSecret
      name: external-restic-config-b2
    path: backblaze-secret.yaml
```

### Patch files

**local.yaml** — points the REST-server backup at the real source PVC:
```yaml
- op: replace
  path: /spec/sourcePVC
  value: <app-name>-app-<volume>
- op: replace
  path: /spec/restic/repository
  value: restic-config-<volume>
```

**local-secret.yaml**:
```yaml
- op: replace
  path: /spec/target/name
  value: restic-config-<volume>
- op: replace
  path: /spec/data/0/remoteRef/key
  value: <APP>_<VOLUME>_REST_REPO_LOCAL
```

**backblaze.yaml**:
```yaml
- op: replace
  path: /spec/sourcePVC
  value: <app-name>-app-<volume>
- op: replace
  path: /spec/restic/repository
  value: restic-config-<volume>-b2
```

**backblaze-secret.yaml**:
```yaml
- op: replace
  path: /spec/target/name
  value: restic-config-<volume>-b2
- op: replace
  path: /spec/data/0/remoteRef/key
  value: <APP>_<VOLUME>_REPO
```

**IMPORTANT: `sourcePVC` must match the app's real PVC name exactly.** A
stale or guessed `sourcePVC` (e.g. assuming a `-data` suffix that the
app-template chart no longer adds) makes the backup a silent no-op — nothing
errors, `ReplicationSource` reports fine, and you only find out when you
need to restore. This exact bug went undetected on `hermes-user1`/
`hermes-user2` for an unknown period. Always confirm the real PVC name via
`oc get pvc -n <app-name>` before writing `local.yaml`, don't infer it from
convention.

### Doppler secrets required per backed-up volume
| Doppler key | Purpose |
|-------------|---------|
| `<APP>_<VOLUME>_REST_REPO_LOCAL` | Restic REST server repository URL, format `rest:http://restic-rest-server-app.restic-rest:8000/<app-name>-<volume>/` |
| `<APP>_<VOLUME>_REPO` | Backblaze B2 restic repository URL |

`RESTIC_PASSWORD`, `BACKBLAZE_KEY_ID`, and `BACKBLAZE_KEY_SECRET` are shared cluster-wide secrets already in Doppler.
