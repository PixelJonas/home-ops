# Adding a New App

## File Structure

Every app under `components-apps/<app-name>/` follows this layout:

```
components-apps/<app>/
├── kustomization.yaml          # lists all resources + ./backup
├── namespace.yaml              # Namespace, sync-wave 0
├── anyuid-rolebinding.yaml     # Only if the container needs a specific UID (see Pod Security below)
├── <app>-chart-app.yaml        # ArgoCD Application pointing at bjw-s app-template
└── backup/
    ├── kustomization.yaml      # lists sub-directories
    └── data/                   # one sub-directory per volume to back up
        ├── kustomization.yaml
        ├── minio.yaml
        ├── minio-secret.yaml
        ├── backblaze.yaml
        └── backblaze-secret.yaml
```

After creating the files, register the app in `bootstrap/overlays/local.home/values-apps.yaml`:

```yaml
  <app-name>:
    annotations:
      argocd.argoproj.io/sync-wave: "300"
    source:
      path: components-apps/<app-name>
```

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

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: <app-name>
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
              - host: <app-name>.apps.altus.janz.digital
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
            storageClass: lvms-vg1   # or nfs for large/shared volumes
            size: 5Gi

  project: cluster-apps
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
    automated:
      prune: true
      selfHeal: true
```

The PVC created for a persistence key `data` on an app named `<app-name>` will be named `<app-name>-data`.
This name is what the volsync backup patches reference as `sourcePVC`.

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
      - altus.janz.digital
      - janz.lan
      - janz.digital
```

Examples in this repo: [n8n.yaml](../components-apps/n8n/n8n.yaml), [firefly-fints-importer.yaml](../components-apps/firefly/firefly-fints-importer.yaml).

---

## Backup (volsync)

Each volume that needs backup gets its own sub-directory under `backup/`. The pattern uses volsync `ReplicationSource` templates from `templates/volsync/` patched via JSON6902.

### `backup/<volume>/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namePrefix: <volume>-
namespace: <app-name>

resources:
  - ../../../../templates/volsync/base       # MinIO (local)
  - ../../../../templates/volsync/backblaze  # Backblaze B2 (offsite)

patches:
  - target:
      group: volsync.backube
      version: v1alpha1
      kind: ReplicationSource
      name: backup
    path: minio.yaml
  - target:
      group: external-secrets.io
      version: v1
      kind: ExternalSecret
      name: external-restic-config
    path: minio-secret.yaml
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

**minio.yaml**
```yaml
- op: replace
  path: /spec/sourcePVC
  value: <app-name>-<volume>
- op: replace
  path: /spec/restic/repository
  value: restic-config-<volume>
```

**minio-secret.yaml**
```yaml
- op: replace
  path: /spec/target/name
  value: restic-config-<volume>
- op: replace
  path: /spec/data/0/remoteRef/key
  value: <APP>_<VOLUME>_REPO_LOCAL
```

**backblaze.yaml**
```yaml
- op: replace
  path: /spec/sourcePVC
  value: <app-name>-<volume>
- op: replace
  path: /spec/restic/repository
  value: restic-config-<volume>-b2
```

**backblaze-secret.yaml**
```yaml
- op: replace
  path: /spec/target/name
  value: restic-config-<volume>-b2
- op: replace
  path: /spec/data/0/remoteRef/key
  value: <APP>_<VOLUME>_REPO
```

### Doppler secrets required per backed-up volume
| Doppler key | Purpose |
|-------------|---------|
| `<APP>_<VOLUME>_REPO_LOCAL` | MinIO restic repository URL |
| `<APP>_<VOLUME>_REPO` | Backblaze B2 restic repository URL |

`RESTIC_PASSWORD`, `MINIO_ACCESS_KEY_ID`, `MINIO_SECRET_ACCESS_KEY`, `BACKBLAZE_KEY_ID`, and `BACKBLAZE_KEY_SECRET` are shared cluster-wide secrets already in Doppler.
