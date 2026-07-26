---
name: migrate-app-to-altus
description: Migrate a single app from Sakaar (Hetzner OpenShift SNO) back to Altus (local OpenShift SNO) using VolSync B2 backup/restore, reversing an earlier migrate-app-to-sakaar move
---

# Migrate App to Altus (Return Trip)

Repeatable procedure to move one application from Sakaar back to Altus using VolSync's Backblaze B2 backup as the transfer medium. This is the mirror image of `migrate-app-to-sakaar.md` — read that skill first if anything here is ambiguous, since most of the mechanism is identical just reversed.

## Why apps are moving back

Full context: `docs/journal/altus-to-sakaar-migration.md` in infra-ops (gitignored, read directly). Short version: every app was drained from Altus onto Sakaar in 2026-07 specifically to allow a clean Altus OS reinstall (clearing a self-inflicted unsupported OpenShift Virtualization feature gate). The reinstall is done, Altus's infra (storage, ArgoCD, External Secrets, cert-manager, etc.) is fully bootstrapped and verified healthy. Jonas's governing principle: **household self-hosted software lives on Altus; Sakaar is for ephemeral workloads, testing/debugging, and GitHub Actions runners.** See the journal's "App-by-App Altus Return Decision" milestone for the authoritative return list and what stays on Sakaar permanently.

## Key differences from the Altus→Sakaar direction

- **Base manifests already target Altus.** Unlike the Sakaar move (which needed a whole new `overlays/sakaar/` per app, because the base pointed at Altus), returning an app means adding it to `bootstrap/overlays/local.home/values-apps.yaml` pointing at the **base** path (`components-apps/<APP_NAME>`, no overlay) — the hostnames (`*.apps.altus.janz.digital`), storage classes (`lvms-vg1`/`synology-nfs-storage`/`synology-iscsi-storage`/`synology-smb-storage`), and access modes in the base are already correct for Altus. No new overlay directory needed. The app's `overlays/sakaar/` directory should be left in place until the app is confirmed healthy back on Altus and drained from Sakaar — don't delete it prematurely.
- **Storage class choice on Altus**: see the journal's storage-class recommendation notes (or ask — this was worked out in a 2026-07-26 follow-up session). Rule of thumb: RWO + latency-critical → `lvms-vg1`. RWO + NAS-backed, want zero cleanup → `synology-iscsi-storage`. RWX (shared, e.g. media stack apps needing concurrent access) → default to `synology-nfs-storage` (backed by `nfs-subdir-external-provisioner`, not synology-csi directly — this is a deliberate exception, see the journal for why). Only use `synology-smb-storage` if you have a specific reason to want synology-csi's CSI features on an RWX volume, since it has `reclaimPolicy: Retain` and needs manual two-step cleanup (`synoshare --del` on the NAS + `oc delete pv`) whenever a PVC using it is deleted.
- **`*_REST_REPO_LOCAL` Doppler values are cluster-agnostic** (same in-cluster DNS service name pattern resolves correctly on both clusters) — no Doppler changes needed for local restic backup targets. `*_REPO`/`*_B2_RESTIC_REPO` (Backblaze B2) values are cluster-agnostic by definition (a bucket in the cloud, not tied to either cluster).
- **NFS-backed apps** (immich, media stack) should point at the Synology's **LAN IP** (`10.10.10.40` via the direct link, or whatever the app's base manifest already specifies) rather than any Tailscale IP used for Sakaar's remote access — Altus is LAN-local to the NAS. Double-check the base manifest already has this right (it should, since it predates the Sakaar move) rather than assuming.
- **`doppler-cluster` ClusterSecretStore exists on both clusters** — most app secrets need no changes. If an app's `ExternalSecret` was patched in its Sakaar overlay to use `doppler-infra` instead of `doppler-cluster`, that patch simply won't exist in the base — verify the base's `secretStoreRef` is `doppler-cluster` and correct if needed.
- **Cloudflare Tunnel repointing**: only `paperless` and `vaultwarden` are publicly exposed (paperless.janz.digital, vault.janz.digital). These need their Cloudflare Tunnel ingress rule repointed from the Sakaar tunnel back to the `home-k8s` tunnel once the app is confirmed healthy on Altus. Every other returning app is LAN-only (`*.apps.altus.janz.digital`), no Cloudflare changes needed.
- **GitHub Actions runners never move.** `tax-agent`'s app returns to Altus, but its CI runner stays on Sakaar permanently (infra-category, not app-category) — don't touch `arc-runners` namespace or the runner scale-sets for this.

## Procedure

### Phase 1: Audit the App on Sakaar

- [ ] **1.1 List the app's PVCs**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc get pvc -n <NAMESPACE>
  ```
  Record each PVC name, size, storageClass, accessMode.

- [ ] **1.2 List existing ReplicationSources**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc get replicationsource -n <NAMESPACE>
  ```
  Identify B2-backed PVCs (names ending `-b2`/`-backup-b2`). Note `lastSyncTime` — trigger a fresh backup regardless of staleness before a migration, don't rely on a possibly-hours-old one.

- [ ] **1.3 Check for CNPG databases**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc get cluster.postgresql.cnpg.io -n <NAMESPACE> 2>/dev/null
  ```
  If present, note the cluster name — CNPG restore is a separate path (Phase 5b), same as the Sakaar-direction skill.

- [ ] **1.4 Identify app-specific Doppler secrets**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc get externalsecret -n <NAMESPACE> -o custom-columns='NAME:.metadata.name,STORE:.spec.secretStoreRef.name,KEYS:.spec.data[*].remoteRef.key'
  ```
  Confirm every referenced key exists in Doppler — **app secrets live in the `homelab`/`home` project, not `infra-ops`/`prd`** (only infra-level secrets like kubeconfigs and cluster tokens live in `infra-ops`/`prd`; mixing these up wastes a round-trip). Keys are cluster-agnostic, but verify rather than assume for anything added specifically during the Sakaar stint.

- [ ] **1.5 Read the app's base Application manifest**

  Read `components-apps/<APP_NAME>/` (the base, not the `overlays/sakaar/` copy) in home-ops. Confirm: ingress hostname is `*.apps.altus.janz.digital`, storage class is one of the Synology/LVM classes (not `lvms-vg1` unless that's genuinely the right choice per the storage-class guidance above), access mode matches the storage class (RWX for NFS/SMB, RWO for LVM/iSCSI), `secretStoreRef` is `doppler-cluster`.

- [ ] **1.6 Audit for undocumented cross-service dependencies**

  Don't trust a prior migration's component list if the app has been under active development since. Read every container's env vars in the Application manifest, not just the ones already known: (a) any URL pointing at `*.svc`/`*.svc.cluster.local` is an **in-cluster DNS dependency** — that target must exist (and be healthy) on the destination cluster before this app will fully work, same class of ordering problem as the litellm/honcho Wave 2 reorder; (b) don't assume a `*_URL`-named secret is external just because a sibling secret with the same naming convention is — verify each one (see 1.6.1 below); (c) grep the rest of home-ops/private-ops for any *other* consumer of a newly-discovered dependency before deciding whether it can move/drain independently.

  - [ ] **1.6.1 Verify secret values without printing them.** Base64-decode the live Secret's field to a file and `shasum -a 256` it, then hash candidate strings (`printf '%s' "$candidate" | shasum -a 256`) the same way and compare — never `cat`/print a decoded value, per the Doppler secret-safety rule.

### Phase 2: Trigger Fresh Backup on Sakaar

- [ ] **2.1 Trigger B2 backup for each PVC**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc patch replicationsource <name>-b2 -n <NAMESPACE> \
    --type merge -p "{\"spec\":{\"trigger\":{\"manual\":\"migrate-$(date +%s)\"}}}"
  ```

- [ ] **2.2 Wait for backup completion**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc get replicationsource <name>-b2 -n <NAMESPACE> -o jsonpath='{.status.lastSyncTime}'
  ```
  Confirm the timestamp advances and `.status.conditions` shows no errors.

- [ ] **2.3 For CNPG databases: trigger a backup**
  ```bash
  KUBECONFIG=<sakaar-kubeconfig> oc apply -f - <<EOF
  apiVersion: postgresql.cnpg.io/v1
  kind: Backup
  metadata:
    name: <cluster-name>-return-backup
    namespace: <NAMESPACE>
  spec:
    method: barmanObjectStore
    cluster:
      name: <cluster-name>
  EOF
  ```
  Wait for `.status.phase` to become `completed`.

### Phase 3: Add App to Altus's values-apps.yaml (pointing at base)

- [ ] **3.1 Add the entry**

  Edit `bootstrap/overlays/local.home/values-apps.yaml`:
  ```yaml
    <APP_NAME>:
      annotations:
        argocd.argoproj.io/sync-wave: "300"
      source:
        path: components-apps/<APP_NAME>
  ```
  Base path, no overlay — see "Key differences" above.

- [ ] **3.2 Validate with kustomize before pushing**
  ```bash
  cd ~/projects/home-ops
  kustomize build bootstrap/overlays/local.home --enable-helm 2>&1 | grep -A20 "<APP_NAME>"
  ```

- [ ] **3.3 Commit and push**
  ```bash
  git add bootstrap/overlays/local.home/values-apps.yaml
  git commit -m "feat(local.home): return <APP_NAME> to Altus

  Assisted-by: Claude Code
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
  git push
  ```

- [ ] **3.4 Wait for ArgoCD to deploy**
  ```bash
  KUBECONFIG=<altus-kubeconfig> oc get application <APP_NAME> -n openshift-gitops -w
  ```
  For stateful apps, the pod will likely fail to start until data is restored (Phase 5) — that's expected, don't treat `Progressing`/`Degraded` at this point as a problem yet.

### Phase 4: (skipped — no overlay-specific config needed on the return trip, since the base already targets Altus)

### Phase 5: Restore Data from B2 Backup

**Skip for stateless apps.**

- [ ] **5.1 Create restic secret on Altus**
  ```bash
  KUBECONFIG=<altus-kubeconfig> oc apply -f - <<EOF
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

- [ ] **5.2 Scale down the app**
  ```bash
  KUBECONFIG=<altus-kubeconfig> oc scale deployment/<APP_NAME>-app -n <NAMESPACE> --replicas=0
  ```
  Wait for pods to terminate.

- [ ] **5.3 Create ReplicationDestination**
  ```bash
  KUBECONFIG=<altus-kubeconfig> oc apply -f - <<EOF
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
  KUBECONFIG=<altus-kubeconfig> oc get replicationdestination <PVC_NAME>-restore -n <NAMESPACE> -w
  ```

- [ ] **5.5 Scale the app back up**
  ```bash
  KUBECONFIG=<altus-kubeconfig> oc scale deployment/<APP_NAME>-app -n <NAMESPACE> --replicas=1
  ```

- [ ] **5.6 Clean up restore resources**
  ```bash
  KUBECONFIG=<altus-kubeconfig> oc delete replicationdestination <PVC_NAME>-restore -n <NAMESPACE>
  KUBECONFIG=<altus-kubeconfig> oc delete externalsecret restic-restore-b2 -n <NAMESPACE>
  ```

### Phase 5b: Restore CNPG Database (if applicable)

- [ ] **5b.1** Create a CNPG Cluster on Altus with `bootstrap.recovery` pointing to the barmanObjectStore backup from Phase 2.3 — app-specific, check the existing Sakaar CNPG cluster manifest for the exact recovery block shape.

### Phase 6: Real Health Check (not just ArgoCD status)

- [ ] **6.1 Pod status**: `oc get pods -n <NAMESPACE>` — all Running/Ready.
- [ ] **6.2 ArgoCD status**: `Synced`/`Healthy`.
- [ ] **6.3 Route/ingress**: `curl -sI https://<app>.apps.altus.janz.digital` returns a valid response.
- [ ] **6.4 Data integrity — do a real comparison, not a vibe check.** For database-backed apps, compare row/record counts against Sakaar's copy before draining Sakaar (`SELECT count(*)` or equivalent), not just "the UI loads." For file-backed apps, spot-check file counts/checksums. This project has been burned before by trusting `pg_stat` estimates or ArgoCD health status alone — see the journal for specifics.

### Phase 7: Repoint Cloudflare Tunnel (paperless/vaultwarden only)

- [ ] **7.1** Only for the two public apps. Repoint the Cloudflare Tunnel ingress rule for `paperless.janz.digital`/`vault.janz.digital` from the Sakaar tunnel back to the `home-k8s` tunnel (Terraform-managed in infra-ops — check `stacks/cloudflare/` for the exact resource). Verify with a real external request after the change, not just `terraform apply` succeeding.

### Phase 8: Drain from Sakaar (only after Phase 6 passes)

- [ ] **8.1** Remove the app's entry from `bootstrap/overlays/sakaar/values-apps.yaml`, commit, push.
- [ ] **8.2** Confirm ArgoCD prunes the Application and namespace on Sakaar (`prune: true` should be set at the relevant level — verify, since this repo's self-heal hierarchy is 3 levels deep and has bitten a `firefly` resurrection before when `prune: false` was set higher up than expected).
- [ ] **8.3** Once confirmed gone, delete the now-unused `components-apps/<APP_NAME>/overlays/sakaar/` directory from git.

### Phase 9: Journal Update

Append to `docs/journal/altus-to-sakaar-migration.md`:
```markdown
### <APP_NAME> returned to Altus — <date>

- **Source:** Sakaar namespace `<NAMESPACE>`
- **PVCs restored:** <list, sizes, storage class chosen on Altus>
- **B2 backup used:** <key name>
- **Data integrity check:** <what was compared, result>
- **Cloudflare Tunnel repointed:** <yes/no — only paperless/vaultwarden>
- **Drained from Sakaar:** <yes/no, date>
- **Issues:** <any problems encountered>
- **Status:** Healthy on Altus
```

## App-Specific Notes

### Simple stateless apps (no PVCs)
Skip Phases 2 and 5. Just Phase 3 (add to values-apps.yaml), Phase 6 (verify), Phase 8 (drain from Sakaar).

### Apps with multiple PVCs
Repeat Phase 5 for each PVC. Scale down once, restore all PVCs, scale back up once.

### Media stack apps specifically
These share a common NFS-backed library volume — check whether the return should be sequenced as one batch (all media-stack apps together) rather than one-by-one, since several likely reference the same underlying share. Read the journal's Wave 3 migration entry for how this was originally structured going the other direction.
