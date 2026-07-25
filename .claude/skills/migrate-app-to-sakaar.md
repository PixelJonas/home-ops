---
name: migrate-app-to-sakaar
description: Migrate a single app from Altus (local OpenShift SNO) to Sakaar (Hetzner OpenShift SNO) using VolSync B2 backup/restore
---

# Migrate App to Sakaar

Repeatable procedure to migrate one application from Altus to Sakaar using VolSync's Backblaze B2 backup as the transfer medium.

## Prerequisites

Before starting, verify:

1. **Kubeconfigs exist** at the paths below (or fetch from Doppler):
   - Altus: `/private/tmp/claude-501/migration-scratch/kubeconfigs/altus.kubeconfig`
   - Sakaar: `/private/tmp/claude-501/migration-scratch/kubeconfigs/sakaar.kubeconfig`

2. **Sakaar infra is healthy** — run:
   ```bash
   KUBECONFIG=/private/tmp/claude-501/migration-scratch/kubeconfigs/sakaar.kubeconfig \
     oc get applications -n openshift-gitops -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
   ```
   All infra apps should show Synced/Healthy. restic-rest-server-app must be Running in namespace `restic-rest`.

3. **App name and namespace** — identify the app to migrate (e.g., `silverbullet` in namespace `silverbullet`).

## Input

When invoking this skill, provide:
- `APP_NAME`: the app name as it appears in `values-apps.yaml` (e.g., `silverbullet`)
- `NAMESPACE`: the Kubernetes namespace (usually same as app name)
- Any special notes (e.g., "has CNPG database", "multiple PVCs")

## Procedure

### Phase 1: Audit the App on Altus

- [ ] **1.1 List the app's PVCs**

```bash
KUBECONFIG=<altus-kubeconfig> oc get pvc -n <NAMESPACE>
```

Record each PVC name, size, storageClass, and accessMode.

- [ ] **1.2 List existing ReplicationSources**

```bash
KUBECONFIG=<altus-kubeconfig> oc get replicationsource -n <NAMESPACE>
```

Identify which PVCs have B2 backups (names ending in `-b2` or `-backup-b2`). Note the last sync time — if stale (>24h old), a fresh backup is needed.

- [ ] **1.3 Check for CNPG databases**

```bash
KUBECONFIG=<altus-kubeconfig> oc get cluster.postgresql.cnpg.io -n <NAMESPACE> 2>/dev/null
```

If a CNPG cluster exists, the database migration is separate from PVC migration — CNPG has its own backup/restore mechanism. Note the cluster name for Phase 4.

- [ ] **1.4 Identify app-specific Doppler secrets**

```bash
KUBECONFIG=<altus-kubeconfig> oc get externalsecret -n <NAMESPACE> -o custom-columns='NAME:.metadata.name,STORE:.spec.secretStoreRef.name,KEYS:.spec.data[*].remoteRef.key'
```

Verify all referenced Doppler keys exist in `homelab/home` (since Sakaar's `doppler-cluster` points there). If any key has a `SAKAAR_` prefix variant needed, note it.

- [ ] **1.5 Read the app's Application manifest**

Read `components-apps/<APP_NAME>/` in the home-ops repo:
- Check `silverbullet.yaml` (or equivalent) for:
  - Ingress hostnames (needs `altus.janz.digital` → `sakaar.janz.cloud`)
  - StorageClass references (needs `synology-*`/`nfs-*` → `lvms-vg1`)
  - AccessMode (NFS uses `ReadWriteMany`, LVM needs `ReadWriteOnce`)
  - Any NFS server IPs or Synology-specific paths
  - ExternalSecret secretStoreRef (should be `doppler-cluster`, works unchanged)

### Phase 2: Trigger Fresh Backup on Altus

- [ ] **2.1 Trigger B2 backup for each PVC**

For each PVC that has a B2 ReplicationSource:

```bash
KUBECONFIG=<altus-kubeconfig> oc patch replicationsource <name>-b2 -n <NAMESPACE> \
  --type merge -p "{\"spec\":{\"trigger\":{\"manual\":\"migrate-$(date +%s)\"}}}"
```

This triggers an immediate one-shot backup to B2.

- [ ] **2.2 Wait for backup completion**

Poll until `lastSyncTime` updates:

```bash
KUBECONFIG=<altus-kubeconfig> oc get replicationsource <name>-b2 -n <NAMESPACE> \
  -o jsonpath='{.status.lastSyncTime}'
```

Wait until the timestamp is newer than before the trigger. Also check `.status.conditions` for errors.

- [ ] **2.3 For CNPG databases: trigger a backup**

If the app has a CNPG cluster:

```bash
KUBECONFIG=<altus-kubeconfig> oc apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: <cluster-name>-migration-backup
  namespace: <NAMESPACE>
spec:
  method: barmanObjectStore
  cluster:
    name: <cluster-name>
EOF
```

Wait for the Backup resource's `.status.phase` to become `completed`.

### Phase 3: Create Sakaar Overlay

- [ ] **3.1 Create the overlay directory**

```
components-apps/<APP_NAME>/overlays/sakaar/
```

If the app doesn't already have an `overlays/` structure, create:
- `kustomization.yaml`
- Patches for hostname, storageClass, accessMode changes

- [ ] **3.2 Handle ingress hostname**

The app's Application CR has inline Helm values with `host: <something>.apps.altus.janz.digital`. Since kustomize can't patch inside a Helm values string-in-a-string, the Sakaar overlay needs a full copy of the Application CR YAML with the hostname changed to `*.apps.sakaar.janz.cloud`.

Pattern: create `<app>-sakaar.yaml` as a complete Application CR copy with:
- Hostname: `*.apps.altus.janz.digital` → `*.apps.sakaar.janz.cloud`
- StorageClass: `synology-csi-*` or `nfs-*` → `lvms-vg1`
- AccessMode: `ReadWriteMany` → `ReadWriteOnce` (for LVM)
- PVC size: keep the same or adjust

The kustomization.yaml should reference this file as a resource (replacing the base).

- [ ] **3.3 Handle backup configuration**

The app's `backup/` directory references the Altus local restic-rest-server. For Sakaar:
- Create `backup/overlays/sakaar/` if needed
- Update the restic repo URL Doppler key references:
  - Change `<APP>_REST_REPO_LOCAL` → create a new Doppler key `<APP>_REST_REPO_SAKAAR` pointing to Sakaar's restic-rest-server (`rest:http://restic-rest-server-app.restic-rest:8000/<app>-data/`)
  - Or if using the same key name, update the Doppler value for Sakaar's context
- B2 backup config can remain unchanged (same B2 bucket)

**Decision point:** If the app only needs B2 backup (no local rest server backup), skip local restic config.

- [ ] **3.4 Handle ExternalSecret store references**

If any ExternalSecret in the base references a Doppler key that lives in `infra-ops/prd` (not `homelab/home`), patch the `secretStoreRef` to use `doppler-infra` in the Sakaar overlay. Most app secrets live in `homelab/home` and work unchanged.

- [ ] **3.5 Validate with kustomize**

```bash
cd ~/projects/home-ops
kustomize build components-apps/<APP_NAME>/overlays/sakaar --enable-helm 2>&1 | head -50
```

Or if the app doesn't use overlays at the component level, validate the full bootstrap:

```bash
kustomize build bootstrap/overlays/sakaar --enable-helm 2>&1 | grep -A5 <APP_NAME>
```

### Phase 4: Add App to Sakaar's values-apps.yaml

- [ ] **4.1 Add the entry**

Edit `bootstrap/overlays/sakaar/values-apps.yaml`, add under `applications:`:

```yaml
  <APP_NAME>:
    annotations:
      argocd.argoproj.io/sync-wave: "300"
    source:
      path: components-apps/<APP_NAME>          # or components-apps/<APP_NAME>/overlays/sakaar if using overlay
```

If the app uses an overlay, the path should point to the overlay directory.

- [ ] **4.2 Commit and push**

```bash
cd ~/projects/home-ops
git add components-apps/<APP_NAME>/overlays/sakaar/ bootstrap/overlays/sakaar/values-apps.yaml
git commit -m "feat(sakaar): add <APP_NAME> for migration

Assisted-by: Claude Code
Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
git push
```

- [ ] **4.3 Wait for ArgoCD to deploy**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc get application <APP_NAME> -n openshift-gitops -w
```

Wait for `Synced`/`Healthy` (or `Progressing` if first deploy). If the app stays `Unknown` or `OutOfSync`, check:
- AppProject sourceRepos includes home-ops
- The path in values-apps.yaml is correct
- Force sync: `oc patch application <APP_NAME> -n openshift-gitops --type merge -p '{"operation":{"sync":{}}}'`

### Phase 5: Restore Data from B2 Backup

**Skip this phase for stateless apps (no PVCs).**

- [ ] **5.1 Create restic secret on Sakaar**

The ReplicationDestination needs a restic secret pointing to the B2 repo. Create an ExternalSecret or direct secret:

```bash
KUBECONFIG=<sakaar-kubeconfig> oc apply -f - <<EOF
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: restic-restore-b2
  namespace: <NAMESPACE>
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: restic-restore-b2
  data:
    - secretKey: RESTIC_REPOSITORY
      remoteRef:
        key: <APP>_B2_RESTIC_REPO
    - secretKey: RESTIC_PASSWORD
      remoteRef:
        key: RESTIC_PASSWORD
EOF
```

Verify the secret was created:
```bash
KUBECONFIG=<sakaar-kubeconfig> oc get secret restic-restore-b2 -n <NAMESPACE>
```

- [ ] **5.2 Scale down the app**

The target PVC must not be in use during restore:

```bash
KUBECONFIG=<sakaar-kubeconfig> oc scale deployment/<APP_NAME>-app -n <NAMESPACE> --replicas=0
```

Wait for pods to terminate.

- [ ] **5.3 Create ReplicationDestination**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: <PVC_NAME>-restore
  namespace: <NAMESPACE>
spec:
  trigger:
    manual: restore-once
  restic:
    repository: restic-restore-b2
    destinationPVC: <PVC_NAME>
    copyMethod: Direct
EOF
```

- [ ] **5.4 Wait for restore completion**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc get replicationdestination <PVC_NAME>-restore -n <NAMESPACE> -w
```

Wait for `.status.lastSyncTime` to be set and `.status.conditions` to show success.

- [ ] **5.5 Scale the app back up**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc scale deployment/<APP_NAME>-app -n <NAMESPACE> --replicas=1
```

- [ ] **5.6 Clean up restore resources**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc delete replicationdestination <PVC_NAME>-restore -n <NAMESPACE>
KUBECONFIG=<sakaar-kubeconfig> oc delete externalsecret restic-restore-b2 -n <NAMESPACE>
```

### Phase 5b: Restore CNPG Database (if applicable)

- [ ] **5b.1 Create a CNPG Cluster with recovery from backup**

The Sakaar CNPG cluster spec should include a `bootstrap.recovery` section pointing to the barmanObjectStore backup. This is app-specific — check the existing CNPG cluster manifest for the correct configuration.

### Phase 6: Health Check

- [ ] **6.1 Verify pod status**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc get pods -n <NAMESPACE>
```

All pods should be Running/Ready.

- [ ] **6.2 Verify ArgoCD status**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc get application <APP_NAME> -n openshift-gitops \
  -o custom-columns='SYNC:.status.sync.status,HEALTH:.status.health.status'
```

Should show Synced/Healthy.

- [ ] **6.3 Verify route/ingress (if applicable)**

```bash
KUBECONFIG=<sakaar-kubeconfig> oc get route -n <NAMESPACE>
```

Check the route hostname resolves and returns a valid response:

```bash
curl -sI https://<app>.apps.sakaar.janz.cloud | head -5
```

- [ ] **6.4 Verify data integrity**

App-specific: check that restored data is visible in the app's UI/API. For example:
- SilverBullet: verify pages are accessible
- Paperless: verify documents appear in search
- Vaultwarden: verify login works

### Phase 7: Journal Update

- [ ] **7.1 Update the migration journal**

Append to `docs/journal/altus-to-sakaar-migration.md` in infra-ops:

```markdown
## <APP_NAME> — <date>

- **Source:** Altus namespace `<NAMESPACE>`
- **PVCs migrated:** <list>
- **B2 backup used:** <key name>
- **Data size:** <approximate>
- **Overlay changes:** <summary of hostname/storageClass/etc changes>
- **Issues:** <any problems encountered>
- **Status:** Healthy on Sakaar
```

## App-Specific Notes

### Simple stateless apps (no PVCs)
Skip Phases 2 and 5 entirely. Just create the overlay (Phase 3), add to values-apps.yaml (Phase 4), and verify (Phase 6).

### Apps with CNPG databases
These have a separate database migration path via CNPG's own backup/restore. The app's data PVC (if any) still uses VolSync. See Phase 5b.

### Apps with NFS-backed PVCs on Altus
Altus apps using `synology-csi` or `nfs-subdir-external-provisioner` storageClass must switch to `lvms-vg1` on Sakaar. This means:
- `ReadWriteMany` → `ReadWriteOnce`
- PVC size should accommodate the data (check `oc get pvc` output)
- No NFS server IP references in Sakaar overlay

### Apps with multiple PVCs
Repeat Phase 5 for each PVC. Scale the app down once, restore all PVCs, then scale back up.

### Ingress considerations
- Altus uses `*.apps.altus.janz.digital`
- Sakaar uses `*.apps.sakaar.janz.cloud`
- DNS records for Sakaar apps are managed via Cloudflare (may need new DNS records if not using wildcard)
