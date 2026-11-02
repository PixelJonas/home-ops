# Storage rebuild playbook

How to back up everything on a storage pool, rebuild the pool, and restore every
workload, without losing data and without the cluster fighting you. Written from
a full rebuild of a local LVMS pool on a single disk-bearing node (107 claims,
about 4.5 days from planning to unfreeze), but the method applies to any change
that destroys a pool: a disk replacement, a StorageClass migration, a VG layout
change, or disaster recovery after losing a pool.

This is the *what* and *why*, with commands only where the exact syntax is the
trap. Per-app procedures live in:

- [volsync-restore.md](volsync-restore.md): one filesystem PVC from restic.
- [database-restore.md](database-restore.md): logical dumps, replay and checks.
- [nextcloud-restore.md](nextcloud-restore.md),
  [knx-ets-volsync-restore-test.md](knx-ets-volsync-restore-test.md).

**A real loss has no rehearsal week.** A planned rebuild has live sources to
re-dump from when a backup turns out to be bad. A real pool loss has only what is
already in the repositories. Read [§11](#11-lessons-for-the-backup-design) before
you need this playbook.

## 1. Control model

- **One coordinator, on a machine that survives the change.** Not a VM whose
  disks are being migrated, and not the node being rebuilt. The coordinator holds
  a lease (single writer), keeps checkpoints off-host, and writes a handover
  prompt before every pause so another session can continue.
- **One evidence root per cutover**, off the pool being rebuilt and access
  controlled (dumps and logs can hold identifiers). Every step writes a verdict
  file: `PASS`, `FAIL`, `ABORT`, `NOT_RUN`, or `PASS-WAIVED` with the waiver's
  reference. Never a forged `PASS`.
- **Subagents and helpers restate the secret rules.** No Secret data, SQL,
  queue payloads, tokens or kubeconfigs in logs, prompts or evidence.
- **Tooling comes from a repo with tests.** Restore scripts written during the
  outage were the largest single source of delay: comparator bugs, log races and
  slot leaks, each found under time pressure.
- **Record before you change.** Every freeze step below snapshots the previous
  state to a file, and the unfreeze restores *from that file*, not from memory or
  from defaults.

## 2. Authorization ledger

Destructive steps are approved in advance, by name, in a ledger the coordinator
keeps next to the evidence:

```text
## <date/time>, user message (verbatim): "<exact text>"
1. Delete the <N> PVCs listed in <allowlist file> (sha256 <prefix>), after <gate>.
2. Tear down and recreate <LVMCluster>/<deviceClass>, after <gate>.
3. DROP DATABASE on newly initialized, empty restore targets only.
...
NOT covered: queue replay or deletion, repository prune/forget, source database
drops, deletion of artifacts or retained evidence, <anything else>.
```

- Number every item and name exact targets. A list of more than a handful goes
  into an allowlist file, referenced by sha256 so it can't change after approval.
- Attach conditions to items ("only when the phase is `unrecoverable`", "after
  that route's PASS") and check them at execution time.
- Standing prohibitions: no finalizer removal, no manual LVM removal outside a
  guarded named list, no repository prune, no queue replay or deletion.
- A successful restore never implies approval to delete anything.
- Cleanup inventories are cross-checked against `Pod.spec.volumes` of live pods
  and against git, not just naming patterns. A live PVC whose name looked like a
  leftover was nearly deleted.
- Keep migration artifacts and repositories unchanged for at least seven healthy
  days after the restore.

## 3. Inventory and route classes

List every PVC on the pool (and every VolumeSnapshot, and every PVC on the pool
without a ReplicationSource) and give each exactly one route:

| Route | Mechanism | Acceptance |
|---|---|---|
| **V** | VolSync RD, `copyMethod: Direct`, pinned snapshot | file manifest + metadata + app read |
| **D** | Logical dump, replayed into a fresh database | structure, exact counts, app read |
| **O** | Encrypted offline copy (tar+GnuPG, or qcow2-LUKS for raw Block) | checksum, decrypt/list, full compare |
| **R** | Recreate empty (caches, CI layers, mover caches) | written per-app OK that empty is fine |
| **I** | Managed re-import (golden images, DataImportCrons) | import Succeeded, DataSource Ready |

- One mechanism and one acceptance check per claim, decided **before** anything
  is destroyed.
- Group coordinated apps into recovery sets (database + files + exports from one
  window, see database-restore.md).
- **Capacity:** keep 25 % margin; plan sparse images at full allocation (restic
  restores them fully allocated); size dump PVCs at 2.5x the dump or more.
- **VolumeSnapshots are not backups.** They live on the pool, die with it, and a
  thin snapshot's LV blocks an LVMS teardown. Export snapshot-only data to another
  backend first, and check that restic retention still holds a point from before
  any known-bad event.
- **Queues and dead-letter state are preserved, not replayed.** Take an offline
  copy if they live on the pool.
- The inventory is point in time. Any new, missing or resized PVC is a reviewed
  change to the inventory, not an ad-hoc decision.

## 4. Gates

Each gate has entry conditions and evidence. Re-verify the freeze at every gate:
drift happened (the GitOps controller came back, CI runner PVCs vanished) and was
only caught because a gate checked again.

| Gate | Entry condition |
|---|---|
| **A** destination | Off-pool backup destination approved, encryption keys in the secret store, helper images pinned |
| **B** rehearsal | One restore test per route class in an isolated namespace, plus a cold rehearsal of the longest VM path (both disks, isolated boot) |
| **C** final backups | Every V/D/O row has a fresh, verified final backup; the original pool is still intact |
| **G** rebuilt storage | New pool Ready; Filesystem and Block provisioning tested on every new class; snapshot class works; nothing references the old pool generation |
| **H/I** VM pre-boot | VM disks restored and compared; identity (firmware UUID, disk names/serials, network identity) and CPU model as intended; fresh post-restore backup done |
| **Unfreeze** | All waves accepted; live vs git diff reviewed |

Gate C is the point of no return. Don't cross it with a missing or failed row.
Passing Gate C does not authorize the teardown; that is its own ledger item.

## 5. Freeze matrix

Stop everything that would write to the pool or undo your changes. Record first,
change second, and unfreeze from the record.

| Component | Freeze | Record before | Unfreeze | Trap |
|---|---|---|---|---|
| ArgoCD | Remove `automated` from every app, then scale the GitOps operator to 0, then the application controller to 0 | `applications-before.json` (all syncPolicies) | See [§9](#9-unfreeze) | The operator restores the controller if you stop only the controller |
| ReplicationSources | Live-patch `trigger.schedule: "0 0 1 1 *"` | RS JSON | GitOps re-applies the git schedule | `trigger: {}` = continuous sync; long `paused` silences alerts |
| ReplicationDestinations | Keep `trigger.manual`; `paused: true` after success | n/a | Delete once accepted | Removing the token = continuous re-restore into a live PVC |
| CronJobs | `suspend: true` | `cronjobs-before.json` | Back to recorded values | Non-git CronJobs aren't restored by sync; `timeZone` CronJobs fire on local time |
| App writers | Scale to 0, workers before databases | `workloads-before.json` | Recorded replicas, in wave order | Keep each database up until its final dump |
| CNPG | `cnpg.io/hibernation=on` once its dump is final | Cluster CRs | Remove the annotation | Without PVCs the Cluster becomes `unrecoverable` (database-restore.md) |
| HCO / CDI | Scale `hco-operator`, `cdi-operator`, `cdi-deployment` to 0; set HCO `spec.workloadSources.enableCommonBootImageImport: false` | HCO spec, replicas | Remove the field first, then HCO, then CDI | DataImportCrons would recreate golden-image PVCs on the default class; keep the DataImportCron objects |
| Image registry | Suspend the ImagePruner, wait out pruner Jobs, `managementState: Unmanaged`, scale `image-registry` to 0 | Config, pruner, Deployment specs | Reverse order, after smoke tests | Never scale the CVO-owned registry operator |
| Monitoring | CVO override for the CMO Deployment, CMO and UWM `prometheus-operator` to 0, operands `paused: true`, StatefulSets to 0 | CVO overrides, replicas, CRs, ConfigMaps | Reverse order; override removed last | Scaling CMO alone is reverted by CVO |
| Nextcloud | Maintenance mode on | n/a | Off only after household acceptance | No CronJob may turn it off early |
| VMs | Stop via git while GitOps runs, or freeze GitOps first | VM spec | Git value | selfHeal reverts `virtctl stop` when git says running; wait for VMI + VolumeAttachments to disappear |
| CI runners | Scale to 0 | replicas | After a real CI job passes | Runner PVCs are recreated (R route) |

### ArgoCD

```bash
oc get applications -n openshift-gitops -o json > applications-before.json
for app in $(oc get applications -n openshift-gitops -o name); do
  oc patch "$app" -n openshift-gitops --type json \
    -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]' 2>/dev/null || true
done
oc scale deployment openshift-gitops-operator-controller-manager -n openshift-operators --replicas=0
oc scale statefulset openshift-gitops-application-controller -n openshift-gitops --replicas=0
```

While frozen, merged PRs don't reconcile and no "Synced" check can pass. Keep a
**deferred-checks list** of every check you had to postpone and verify each one
at unfreeze. Merge PRs that change storage layout (new StorageClass, VG layout,
PVC classes) *before* unfreezing, so the first sync applies the new world rather
than the old one.

### Monitoring

The Cluster Monitoring Operator's Deployment is owned by the ClusterVersion, and
its operands have no `replicas` field, so plain scaling is undone. Add exactly one
override entry, merged into the existing array (never replace other entries):

```bash
oc get clusterversion version -o json | jq '{spec:{overrides:(.spec.overrides // null)}}' > cvo-before.json
jq '.spec.overrides = (((.spec.overrides // []) + [
  {group:"apps",kind:"Deployment",namespace:"openshift-monitoring",
   name:"cluster-monitoring-operator",unmanaged:true}]) | unique)' cvo-before.json > cvo-patch.json
oc patch clusterversion version --type merge --patch-file cvo-patch.json
```

Then scale CMO to 0 and confirm it stays at 0 for two minutes, scale the UWM
`prometheus-operator` to 0, set `paused: true` on the Prometheus, Alertmanager and
ThanosRuler CRs, and scale their StatefulSets to 0. An override sets the cluster
`Upgradeable=False`; remove it last, restoring `cvo-before.json` exactly.

Monitoring doesn't have to come back first. If your health gates read kubelet
stats, node-exporter and etcd directly, application waves can run while
monitoring is still down.

### Registry

Suspend `imagepruners.imageregistry.operator.openshift.io/cluster` first (it has
no `managementState`), wait for running pruner Jobs to finish, then set
`configs.imageregistry.operator.openshift.io/cluster` to `Unmanaged` and scale
`deployment/image-registry` to 0. Return to `Managed` only after `/healthz`, a
digest-verified pull, a scratch push/pull and a healthy ClusterOperator. A
registry that only holds re-pullable images may waive its file comparison
(record `PASS-WAIVED` with the ledger reference); its runtime smoke tests still
must pass. Golden-image DataImportCrons with `pullMethod: node` never stored
their blobs in the registry, so don't expect to find them there.

## 6. Final backups

- Writer-free window per app: stop writers, keep the database up, dump, then
  hibernate or stop the database.
- Dumps over the database Service with structural validation
  (database-restore.md). Trigger the CronJob as a Job and wait for *that* Job.
- Trigger every RS leg (local and off-site), wait for `lastManualSync` == token
  and `Successful`, then remove only the token. The parked schedule stays.
- Offline copies are encrypted end to end (`tar | gpg` straight to the
  destination, qcow2-LUKS for raw Block); plaintext never lands on the NAS. The
  passphrase is a Secret mounted as a file (see the note on `fsGroup` in
  [Appendix C](#appendix-c-cold-vm-disk-backup-helper)).
- `restic check`: after the final backup, before any destructive step, one full
  `--read-data` at a time, outside any mover run. Record snapshot IDs with
  `restic snapshots --json`. No forget or prune until the window closes.
- VM disks: both disks of a VM in one stopped-writer window, as one recovery set.
  The VMI and both VolumeAttachments must be gone before the copy starts.

## 7. Teardown and rebuild (LVMS)

LVMS refuses to delete an LVMCluster while PVCs use its classes, and its
vg-manager then removes the thin pool. **Any thin LV still in the pool** (a
snapshot LV, an orphan LV with no PV) makes that removal fail and the deletion
wedges. The only ways out are finalizer removal or a manual `lvremove`, so make
sure it can't wedge:

1. Export snapshot-only data and delete VolumeSnapshots (ledger items).
2. Quiesce remaining consumers (CNPG hibernation, StatefulSets to 0, HCO import
   off). Owners recreate deleted PVCs otherwise.
3. Delete the allowlisted PVCs. Pods in `Succeeded` state still hold
   `pvc-protection`; delete those pods by name (a ledger item).
4. Match `lvs` against the `volumeHandle` of every remaining PV. Orphan LVs are
   removed only from a guarded, named list after re-checking that no
   LogicalVolume CR or PV references them and their open count is 0.
5. Confirm `lvs` shows only the thin pool. Last abort point.
6. Delete the LVMCluster; save its YAML first. If it stalls, stop and escalate;
   no finalizer edits.
7. Recreate with `deviceSelector.paths` from `/dev/disk/by-id/` (kernel names
   swap between boots). One VG per disk class. Leave unallocated headroom so a
   disk can leave the VG later without a rebuild (the rebuild in this playbook
   happened because a `pvmove` lacked about 560 GiB of free extents).
8. Gate G: deviceClasses and their disks, StorageClasses and snapshot classes,
   thin pool usage, a write test (Filesystem + Block PVC on every class, a
   snapshot and a restore) in a throwaway namespace.

## 8. Restore waves

- **Two lanes.** A lane is one heavy stream. Hold stream slots explicitly (a
  lock file per slot) and release them only after the verdict. DRY runs hold no
  slot.
- **Health gates** before and during every stream: root filesystem (`/var`)
  under 80 %, node iowait under 20 %, etcd WAL fsync p99 under 100 ms. Sample
  often (5 s) over a minute; two consecutive failures stop the data process with
  `SIGSTOP`, never by deleting it. A pulse throttle (`SIGCONT` for about a second,
  then `SIGSTOP`) kept a long image conversion moving on a contended disk where a
  plain on/off throttle tripped the gate.
- **Order:** platform first (registry, then the longest VM path, which gates the
  most people), shared services next (model gateway, vector store, document
  store, memory service, document conversion), then their dependents, with
  delayed writers last (CI runners, schedulers and cron, queue workers,
  downloads, crawlers).
- **Pre-create destination PVCs** with original names, modes and sizes right
  after Gate G (they stay `Pending` on `WaitForFirstConsumer` classes).
- **Per item:** restore PASS, app gate PASS, post-restore backup PASS (fresh
  backup from the restored data, token removed, schedule re-armed), then the next
  item. Count catch-up backups as streams.
- Keep the deferred-checks list current; the GitOps checks in it can only pass
  after unfreeze.

## 9. Unfreeze

1. **Stale operations.** An Application whose `status.operationState.phase` is
   still `Running` from before the freeze resumes that operation, at its *old*
   revision, as soon as the controller starts. That regressed two apps to a
   three-day-old commit. Before scaling up, list them:

   ```bash
   oc get applications -n openshift-gitops -o json | jq -r \
     '.items[] | select(.status.operationState.phase == "Running") | .metadata.name'
   ```

2. Scale the GitOps operator to 1; it brings the application controller back.
   Watch that only the apps that were automated before the freeze act, and that
   no PVC UIDs change.
3. For every frozen app: hard refresh, diff live against git, decide anything
   unexpected. Objects created out of band (restored PVCs adopted by DataVolumes,
   labels added by restore tools) need `ignoreDifferences` (with
   `RespectIgnoreDifferences=true`) before sync, never Replace or Force.
4. Re-enable **children before parents**: sync at the full remote SHA, require
   `status.sync.revision == status.operationState.syncResult.revision == remote
   HEAD`, wait Synced/Healthy, then restore the recorded `automated` value and diff
   PVC UIDs. Parents last; they re-apply child sync policies.
5. CronJobs back to the recorded suspend state (including ones not in git).
6. RS schedules come back with each app's sync. Re-arm in batches; every RS
   whose last sync predates its last tick runs immediately.
7. Work through the deferred-checks list.

## 10. Close-out

- Audits: no RS/RD with a manual token, empty trigger, parked schedule or
  `paused`; CronJob suspend states equal the record; PVC UIDs as expected; every
  Application Synced/Healthy with its recorded policy; alerts clear. Stale VolSync
  series from deleted test namespaces keep alerts firing until the volsync
  operator pod restarts; after a restart, check paused RS separately, since they
  stop reporting.
- Watch the first scheduled backups and dumps the next morning, per RS and per
  CronJob, including the ones fixed during the window.
- Cleanup only from ledger items, YAML saved first. Delete temporary encryption
  keys from the secret store once the artifacts that need them are gone.
- Image prune on the node if its root disk filled during the window.
- Sync docs and rules, write the retrospective and a frozen journal entry.

## 11. Lessons for the backup design

What hurt most in a real rebuild, and what makes the next one (or a real loss)
shorter:

- **IO, not bandwidth, sets recovery time** when etcd shares the data node's
  root disk: a two-stream limit and throttling turned a one-day estimate into
  about ten hours of restore after the rebuild, and verification and retries
  dominated the downtime, not copying.
- **Backups faithfully preserve damage.** Checksum mismatches and missing blobs in
  two apps predated the migration and went unnoticed until a full compare ran.
  Run app-level integrity checks on a schedule.
- **Every PVC needs a route.** PVCs without a ReplicationSource were handled as
  "regenerate", which is only fine if someone decided that per app.
- **Dumps need validation at capture**, not at restore: a truncated dump had been
  backed up for weeks.
- **Restore drills on a schedule**, one app per route class, with the same
  acceptance checks.

## Appendix A: comparator pitfalls

- The PVC root directory differs between source and restore (mtime, sometimes
  mode); compare it separately.
- `restic restore` lists unchanged directories as `restored` with 0 B.
- `restic ls` paths are relative to the snapshot root, which differs between
  movers and helpers; accept one layout per snapshot.
- `qemu-img compare -p` mixes progress output into the lines you grep; run it
  without `-p` and gate on the exit code.
- `getfacl -h` prints help, not ACLs. Self-test the ACL tool on a known file.
- Kubelet log rotation truncates long helper logs: stream with `logs -f` into
  the evidence and assert start/end markers.
- `| head` under `set -o pipefail` fails with SIGPIPE (rc 141).
- Writing a script as `.new` and `mv`-ing it drops the exec bit; use `install -m`.
- Don't read a helper's log before its last line (the hash) is written; poll for
  every expected line.

## Appendix B: recovery-time reality

| Item | Planned | Actual | Limiter |
|---|---|---|---|
| 300 GiB raw VM root restore (convert + full compare) | about 8 h | about 4 h | throttling under the iowait gate |
| One VM, end to end (stop to boot) | 1 day | about 42 h | failed rehearsal paths, gates |
| One mid-size dump import | minutes | blocked for hours | shared-disk contention |
| 107 claims after the rebuild | 1 day | about 10 h | two-stream limit, tooling fixes |
| Planning to unfreeze | | about 4.5 days | |

## Appendix C: cold VM disk backup helper

A raw Block VM root is best captured as a self-contained, sparse, encrypted qcow2
image, after the VM is stopped and its disk is detached. The helper runs as a Job
with its own ServiceAccount, a pinned SCC that grants only the needed
capabilities, the source attached read-only as a block device, and the passphrase
as a Secret file.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: vm-root-cold-backup-<ts>
  namespace: <vm-namespace>
spec:
  backoffLimit: 0
  template:
    metadata:
      annotations:
        openshift.io/required-scc: <helper-scc>   # check the admitted openshift.io/scc
    spec:
      restartPolicy: Never
      serviceAccountName: <helper-sa>
      automountServiceAccountToken: false
      securityContext:
        runAsUser: 0          # no fsGroup: it would change the Secret file's mode
      containers:
        - name: qemu
          image: <pinned image with qemu-img>
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
          command: ["/bin/bash", "-ec"]
          args:
            - |
              set -o pipefail
              umask 077
              test -b /dev/source
              test ! -e "/backup/$ARTIFACT.qcow2"
              KEY="--object secret,id=k,file=/run/secrets/backup/passphrase"
              qemu-img convert $KEY -f raw -O qcow2 \
                -o cluster_size=2M,preallocation=off,encrypt.format=luks,encrypt.key-secret=k \
                /dev/source "/backup/$ARTIFACT.qcow2"
              OPTS="driver=qcow2,file.driver=file,file.filename=/backup/$ARTIFACT.qcow2,encrypt.format=luks,encrypt.key-secret=k"
              qemu-img check $KEY --image-opts "$OPTS"
              qemu-img info  $KEY --backing-chain --image-opts "$OPTS"   # no backing file
              qemu-img compare $KEY --image-opts "$OPTS" \
                "driver=raw,file.driver=host_device,file.filename=/dev/source"
              sha256sum "/backup/$ARTIFACT.qcow2" > "/backup/$ARTIFACT.qcow2.sha256"
              stat -c '%n apparent=%s allocated-blocks=%b' "/backup/$ARTIFACT.qcow2"
              sync
          env:
            - name: ARTIFACT
              value: <vm>-root-<YYYYMMDDTHHMMSSZ>
          volumeDevices:
            - name: source
              devicePath: /dev/source
          volumeMounts:
            - name: backup
              mountPath: /backup
            - name: key
              mountPath: /run/secrets/backup
              readOnly: true
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: source
          persistentVolumeClaim:
            claimName: <vm-root-pvc>
            readOnly: true
        - name: backup
          persistentVolumeClaim:
            claimName: <off-pool-artifact-pvc>   # NFS or another backend, never the pool being rebuilt
        - name: key
          secret:
            secretName: <passphrase-secret>      # from an ExternalSecret
            defaultMode: 0400
        - name: tmp
          emptyDir: {}
```

- The restore is the mirror image: `qemu-img convert --image-opts ... -O raw -S 4k
  "$OPTS" /dev/target` into a new Block PVC of at least the virtual size, then the
  same `compare` against the target, then a read-only guest inspection
  (`guestfish --ro`: partition table, filesystems, a known file).
- Pair it with the VM's data-disk backup from the same stopped window, and prove
  the pair with one isolated two-disk boot (no production network identity, a
  different MAC, no passthrough devices) before the pool is torn down.
- `qemu-img compare` reads the whole disk; it is the integrity gate, so don't skip
  it to save time. Gate on its exit code.
- **Secret file modes:** with `fsGroup`, the kubelet makes Secret files
  group-owned and group-readable, so `defaultMode: 0400` shows as `0440`. A root
  helper without `fsGroup` reads a 0400 file; an arbitrary-UID pod without
  `fsGroup` can't. Assert the mode you actually configured.
- A direct NFS `volumes[].nfs` source may fail SCC admission for a restricted
  helper; a pre-created NFS PV/PVC pair with `Retain` avoids that and keeps the
  artifact through namespace cleanup.
