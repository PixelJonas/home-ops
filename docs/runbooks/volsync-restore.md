# VolSync restore runbook

Restore one application's filesystem PVC from its restic repository with a
VolSync `ReplicationDestination` (RD), verify the result without changing it,
then return the app and its backup schedule to normal.

Related runbooks:

- [database-restore.md](database-restore.md): logical database dumps (the dump
  PVC itself is restored with this runbook first).
- [storage-rebuild-playbook.md](storage-rebuild-playbook.md): restoring many apps
  after a storage pool rebuild (freeze, waves, unfreeze).
- [nextcloud-restore.md](nextcloud-restore.md) and
  [knx-ets-volsync-restore-test.md](knx-ets-volsync-restore-test.md): app-specific
  procedures built on this pattern.

The backup side lives in `templates/volsync/` (ReplicationSource + ExternalSecret
per backend) and `templates/replicationdestination.yaml` (the RD shape).

## Rules that apply to every restore

- **Never print Secrets.** Repository URLs and passwords come from the app's
  existing `ExternalSecret`. Check the `SecretSynced`/`Ready` condition, never the
  Secret data.
- **One mechanism, one acceptance check, decided before you start.** Write down
  which snapshot you restore, into which PVC, and what "restored correctly" means
  for this app (file manifest, row counts, application read) before creating the
  RD.
- **A green mover is not a restore.** `latestMoverStatus.result: Successful`
  proves restic wrote files. Content, ownership and the app's ability to read its
  data are separate checks.
- **Don't prune or `forget` during a restore window.** Every scheduled or manual
  backup applies the ReplicationSource `retain` policy, so extra syncs can rotate
  out the snapshot you validated. Record snapshot IDs before you start.
- **Count the restore as a data stream.** On a node whose root disk also holds
  etcd, run at most two heavy streams at once (restores, dump imports, full
  `restic check --read-data`, catch-up backups, large copies). See the health
  gates in the playbook.

## 1. Prepare

1. **Stop GitOps from undoing your work.** Disable `automated` on the parent
   Application first, then on the app's Application (the parent re-applies the
   child's sync policy otherwise). Record the previous `syncPolicy` so you can
   restore it exactly.

   ```bash
   oc get application <app> -n openshift-gitops -o jsonpath='{.spec.syncPolicy}' > <evidence>/<app>-syncpolicy-before.json
   oc patch application <parent> -n openshift-gitops --type json \
     -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
   oc patch application <app> -n openshift-gitops --type json \
     -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
   ```

2. **Stop every writer** of the destination PVC: the app, its workers, CronJobs
   (`suspend: true`, record the previous value), and the app's own
   ReplicationSource if it would fire during the restore (park it, see
   [Parking a ReplicationSource](#parking-a-replicationsource)).

3. **Prove the PVC is detached.** A Deployment at 0 replicas is not proof:

   ```bash
   PV="$(oc get pvc <pvc> -n <ns> -o jsonpath='{.spec.volumeName}')"
   oc get pods -n <ns> -o json | jq -r --arg c <pvc> \
     '.items[] | select(any(.spec.volumes[]?; .persistentVolumeClaim.claimName == $c)) | .metadata.name'
   oc get volumeattachments -o json | jq -r --arg pv "$PV" \
     '.items[] | select(.spec.source.persistentVolumeName == $pv) | .metadata.name'
   ```

   Both lists must be empty before a `Direct` restore. For a VM disk, the VMI must
   be gone too; a guest shutdown request or `runStrategy: Halted` alone is not
   detach evidence.

4. **Pick and record the snapshot.** List snapshots from a throwaway restic Job
   (see [Checking a repository](#checking-a-repository)) and record the snapshot
   ID, its time and its path. Pin the RD to that point with `restoreAsOf` (the
   newest snapshot at or before that time is used) or `previous` (skip the N most
   recent). An unpinned RD restores whatever is newest when the mover starts.

5. **Prepare the destination PVC.** It must exist, be `Bound` (or `Pending` on a
   `WaitForFirstConsumer` class), and have the intended name, StorageClass,
   volume mode, access mode and at least the source's requested size. Keep the
   original PVC name when restoring in place, so the workload and its
   ReplicationSource need no change. Do not set a `dataSource`/`dataSourceRef` on
   an empty VolSync destination.

6. **Capture the before state** you will compare against: the source manifest if
   the source still exists (path, type, mode, numeric UID/GID, size, sha256 at a
   declared full or sampled scope), the workload's `securityContext`, and the
   PVC's UID.

## 2. Restore

Use `copyMethod: Direct` into the prepared PVC:

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: <app>-<component>-restore
  namespace: <ns>
spec:
  trigger:
    manual: restore-<YYYYMMDDTHHMMSSZ>
  restic:
    repository: restic-config-<component>        # Secret from the app's ExternalSecret
    destinationPVC: <pvc>
    copyMethod: Direct
    restoreAsOf: "<RFC3339 time of the chosen snapshot>"
    # moverSecurityContext: see "Ownership and modes" below
```

**Never remove `trigger.manual` from an RD.** An RD with `trigger: {}` (or no
trigger) syncs continuously: it re-restores into the PVC over and over, also
after the app has started writing to it. Tooling that "cleans up" the manual
token after success causes exactly this. When the restore has finished, either
set `spec.paused: true` or delete the RD, and do it **before** the app starts.

Wait for the current operation, not just any status:

```bash
oc get replicationdestination <rd> -n <ns> -o json | jq '{
  manual: .spec.trigger.manual,
  lastManualSync: .status.lastManualSync,
  lastSyncTime: .status.lastSyncTime,
  result: .status.latestMoverStatus.result,
  conditions: [.status.conditions[]? | {type, status, reason}]}'
```

Done means `lastManualSync` equals your token and `latestMoverStatus.result` is
`Successful`. `Synchronizing=False` on its own also matches "never started" and
"failed". Then pause or delete the RD:

```bash
oc patch replicationdestination <rd> -n <ns> --type merge -p '{"spec":{"paused":true}}'
```

For a restore into a **scratch namespace** (a rehearsal or a side-by-side check),
recreate the app's ExternalSecret *spec* there (same Doppler key references, new
metadata), wait for it to be Ready, and restore into a scratch PVC. Never copy the
generated Secret object.

## 3. Verify without changing anything

Inspect the restored PVC with a read-only mount before the app starts:

- Mount it `readOnly: true` in a helper. Don't give the helper an `fsGroup`: the
  kubelet would rewrite group ownership on the volume you are trying to check.
- Compare like with like. Paths must be relative to the same root on both sides
  (restic lists nodes relative to the snapshot root, a live mount shows its own
  mount point), and the PVC root directory is compared separately from its
  contents (mount points differ in mtime and sometimes in mode).
- Compare path, type, mode (including setgid and sticky bits), numeric UID/GID,
  regular-file size and sha256 at the declared scope. Small config/state volumes
  get full checksums. Large trees may be sampled; say so in the evidence.
- SELinux MCS categories are per namespace. A scratch namespace gets different
  ones, so don't compare them byte for byte. Check that the intended workload
  identity can read and write instead.
- Sparse files (VM images) come back fully allocated from restic. Compare logical
  content and apparent size, and plan capacity for full allocation.
- `restic restore --dry-run`/`--verbose` reports directories as `restored` with
  0 B even when nothing changed. Don't count those lines as differences.

### Ownership and modes

The mover's identity decides who owns restored files, and `fsGroup` changes them
further. Plan this per app:

- Without a `moverSecurityContext`, the result depends on the namespace: a
  privileged mover (namespace annotation `volsync.backube/privileged-movers`)
  preserves the snapshot's numeric owners, an unprivileged one can't. With a
  `moverSecurityContext`, files are written as that UID/GID. Check the result
  against the before manifest; don't assume either.
- `fsGroup` on a mover or inspector group-chowns everything it mounts and adds
  group bits. When exact ownership is part of the data, omit `fsGroup` (and
  `fsGroupChangePolicy`) and set only `runAsUser`/`runAsGroup`/`runAsNonRoot`.
- A root helper that `chown`s a directory to a group it is not a member of drops
  the setgid bit. Add the destination GID to `supplementalGroups` (or grant
  `CAP_FSETID`), and compare setgid bits explicitly.
- Fix only the enumerated differences, path by path. Never `chown -R` a whole
  application volume to a guessed UID. Prove content hashes are unchanged after
  the fix, then re-run the comparison.
- Known fixed-UID cases: KubeVirt disk images and vTPM state are `107:107`;
  NFS exports with `root_squash` need the mover/helper to run as the file owner
  (see `nextcloud-restore.md`).

### Helper pods and SCCs

A bare Pod created by a cluster-admin identity is admitted against that
identity's SCCs as well as its ServiceAccount's, so it can silently land on a
privileged SCC. Controller-created Pods only use their ServiceAccount. Run
helpers as a Job with a dedicated ServiceAccount and pin the SCC:

```yaml
spec:
  template:
    metadata:
      annotations:
        openshift.io/required-scc: restricted-v2   # or the one SCC the helper needs
```

Check `metadata.annotations["openshift.io/scc"]` on the admitted pod before any
`exec` or write, and stop on a mismatch.

### Restores that create PVCs from another PVC

A PVC with a cross-namespace `dataSourceRef` binds as an **empty** volume when the
`CrossNamespaceVolumeDataSource` feature gate is off: the API server drops the
field and the provisioner creates a blank disk. After creating such a PVC, fail if
`spec.dataSourceRef` is null, and gate on content (checksum, partition table or a
known file) before booting or accepting it.

## 4. Return to service

1. Make sure the RD is paused or deleted.
2. Start the app with its recorded replica counts, dependencies first. Check an
   application-level read of existing data, not just pod readiness.
3. **Take a fresh backup from the restored PVC.** Trigger the app's
   ReplicationSources manually, wait for `lastManualSync` == token and
   `Successful`, then remove only the manual token and confirm the schedule and
   `status.nextSyncTime`:

   ```bash
   oc patch replicationsource <rs> -n <ns> --type merge \
     -p '{"spec":{"trigger":{"manual":"post-restore-<ts>"}}}'
   # wait ...
   oc patch replicationsource <rs> -n <ns> --type json \
     -p '[{"op":"remove","path":"/spec/trigger/manual"}]'
   oc get replicationsource <rs> -n <ns> -o jsonpath='{.spec.trigger.schedule}{"\t"}{.status.nextSyncTime}{"\n"}'
   ```

4. Resume CronJobs to their recorded suspend state.
5. Restore the Applications' recorded `syncPolicy`, child first, after a hard
   refresh shows the child Synced (live matches git).
6. Delete temporary objects (helper Jobs, scratch ExternalSecrets, the RD once
   the restore is accepted) after saving their YAML to the evidence directory.
7. Audit that nothing was left in a maintenance state:

   ```bash
   oc get replicationsource,replicationdestination -A -o json | jq -r '.items[]
     | select(.spec.trigger.manual or .spec.trigger == {} or .spec.trigger == null or .spec.paused)
     | "\(.kind) \(.metadata.namespace)/\(.metadata.name)"'
   ```

   The list must be empty (or contain only RDs you deliberately kept paused).

## Parking a ReplicationSource

To stop an RS from running during maintenance, live-patch a schedule that won't
fire soon:

```bash
oc patch replicationsource <rs> -n <ns> --type merge \
  -p '{"spec":{"trigger":{"schedule":"0 0 1 1 *"}}}'
```

- Never park with `trigger: {}`: that means "sync continuously".
- Avoid long `spec.paused`: a paused RS stops reporting out-of-sync status, so the
  stale-backup alerts go quiet while nothing is backed up.
- Record the git schedule first. GitOps restores it on the next sync of the app.
- **Re-arming fires a catch-up backup at once** when `lastSyncTime` predates the
  last tick of the restored schedule. Re-arm many RS in small batches and count
  each catch-up as a data stream.

## Checking a repository

`restic check` takes an exclusive lock on the repository:

- Run it outside the RS schedule and never while that repository's mover runs.
- Run full `--read-data` checks one repository at a time; each is a data stream.
- A check killed mid-run leaves a stale lock, and the next scheduled mover fails
  with `repository is already locked`. Clear it with
  `oc patch replicationsource <rs> -n <ns> --type merge -p '{"spec":{"restic":{"unlock":"'"$(date +%s)"'"}}}'`
  (only removes stale locks).

A throwaway check Job, using the existing repository Secret and a disposable
cache:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: restic-check-<rs>-<ts>
  namespace: <ns>
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: restic
          image: <the restic image the movers use>
          envFrom:
            - secretRef:
                name: restic-config-<component>
          env:
            - name: RESTIC_CACHE_DIR
              value: /cache
          command: ["/bin/sh", "-ec"]
          args:
            - |
              restic snapshots --json > /dev/null
              restic snapshots --latest=3
              restic check --read-data-subset=5%
          volumeMounts:
            - name: cache
              mountPath: /cache
      volumes:
        - name: cache
          emptyDir: {}
```

Its log contains snapshot IDs and paths, not credentials, but review it before
copying it anywhere. Use the same restic version the movers use; don't upgrade
tools in the middle of a recovery.

## Large trees and SELinux relabeling

CRI-O relabels a volume's SELinux context on every pod start. On a tree with
millions of files that can exceed the container-create timeout, so the mover
loops in `CreateContainerError` and never runs. Options, narrowest first:

- `seLinuxChangePolicy: MountOption` where the volume type supports it (CSI
  drivers that support SELinux mount; not local PVs).
- Run that one mover (and the app pod) with SELinux type `spc_t`, through a
  dedicated SCC bound only to its ServiceAccount, so CRI-O skips the relabel.
  See `components-apps/influxdb3/` for the pattern. A restore into such a PVC
  needs the same SCC binding for the RD mover.

## VM disks

- Restore every disk of a VM from **one recovery set** (the same stopped-writer
  window). A root disk and a data disk from different points are not a valid pair.
- Keep `spec.template.spec.domain.firmware.uuid` byte-identical and keep the
  persistent-state PVC labelled `persistent-state-for: <vm>` so vTPM/NVRAM state
  (BitLocker, measured boot) is adopted.
- Verify `disk.img`, NVRAM and vTPM files, their `107:107` ownership and SELinux
  accessibility before the first boot.
- A restored PVC that a git-managed `DataVolume` owns will differ from the DV spec
  forever (DV specs are immutable). Add `ignoreDifferences` on the DataVolume
  `/spec` plus `RespectIgnoreDifferences=true` to that app before re-enabling
  sync, and never Replace/Force-sync it: that can delete and recreate the DV and
  its PVC.
- For a raw Block root disk, a sparse encrypted qcow2 copy is a better backup
  than a restic file snapshot. See the cold VM backup appendix in the playbook.
