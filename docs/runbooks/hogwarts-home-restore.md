# Hogwarts VM: restore the /home disk

How to get Julia's `/home` back from VolSync, as a rehearsal (the restore
drill) or for real. Built on [volsync-restore.md](volsync-restore.md) (rules,
ownership, RD shape) and the knx-ets pattern
([knx-ets-volsync-restore-test.md](knx-ets-volsync-restore-test.md)). Design:
infra-ops `docs/agents/hogwarts-spec-2026-11-06.md` section 10.

## What is backed up, what is not

| Disk | PVC | Backup | After loss |
|---|---|---|---|
| `/home` (both accounts, Hermes state of Julia's agents) | `hogwarts-home`, 60Gi Filesystem, holds one `disk.img` | ReplicationSource `home-backup`, daily 04:15, restic REST server (repo `HOGWARTS_HOME_REST_REPO_LOCAL`, Doppler `homelab/home`), copyMethod Snapshot | this runbook |
| root disk (OS, packages) | `hogwarts-rootdisk`, Block | none, by design | rebuild, see the last section |

restic stores the whole `disk.img` (the guest's ext4 image, chunk-deduplicated),
not single files. A restore always brings back the entire `/home` disk at one
point in time; a single file is fetched afterwards from a scratch copy (part A).

Each snapshot is crash-consistent (an LVM snapshot of the running VM, no guest
freeze): ext4 replays its journal on boot, SQLite (`state.db`, kanban) recovers
from its WAL. Expect the state of up to one day before the snapshot time.

## Rules for this VM

- **Never print Secrets**, including the restic repo URL. Check `SecretSynced`.
- **Never boot two copies of Julia's agents.** A restored `/home` contains her
  Hermes profiles with live bot tokens. A second gateway polling the same
  Telegram bots steals updates from the production one, and every `hermes`
  CLI call auto-spawns a gateway if none runs. The scratch VM in part A
  therefore has no internet egress at all (Telegram and Tailscale
  unreachable), a fresh root disk (no Ansible run, no linger, no Tailscale
  join, so no user service starts), and its copy of the profile `.env` files
  has the bot tokens emptied before any `hermes` command. Never run the
  infra-ops Ansible against it.
- **A restore over the live disk is destructive.** Part B needs Jonas's
  approval of the whole plan first (target snapshot, the pre-restore backup,
  revert path), like any restore over live data.
- Restore from **one point in time**. Don't mix a restored `/home` with files
  copied from another snapshot.

## Part A: rehearsal in a scratch namespace (restore drill)

The production VM keeps running. Budget about one hour (60Gi image).

1. **Preflight.** `oc get replicationsource home-backup -n hogwarts` shows a
   recent `LAST SYNC`; the lvms thin pool has room for a 60Gi copy
   (`oc debug node/altus -q -- chroot /host lvs -o lv_name,data_percent vg1`).
   List the snapshots with the check Job from
   [volsync-restore.md](volsync-restore.md#checking-a-repository)
   (`restic-config-home`) and record ID and time of the one you restore.

2. **Namespace and restic Secret.** Create `hogwarts-restore`. Recreate
   the ExternalSecret spec there (never copy the generated Secret):

   ```yaml
   apiVersion: external-secrets.io/v1
   kind: ExternalSecret
   metadata:
     name: home-external-restic-config
     namespace: hogwarts-restore
   spec:
     secretStoreRef: {kind: ClusterSecretStore, name: doppler-cluster}
     target: {name: restic-config-home}
     data:
       - secretKey: RESTIC_REPOSITORY
         remoteRef: {key: HOGWARTS_HOME_REST_REPO_LOCAL}
       - secretKey: RESTIC_PASSWORD
         remoteRef: {key: RESTIC_PASSWORD}
   ```

   Add `hogwarts-restore` to the mover namespaces of
   `components-infra/restic-rest-server/networkpolicy.yaml` for the duration
   (a live patch is reverted by selfHeal; use a short-lived PR, or freeze the
   `restic-rest-server` app per volsync-restore.md step 1), otherwise the
   mover hangs on connect.

3. **Destination PVC** `home-test`: 60Gi, `lvms-vg1`, `ReadWriteOnce`,
   `volumeMode: Filesystem`, no `dataSource`.

4. **Restore** (RD shape from `templates/replicationdestination.yaml`):

   ```yaml
   apiVersion: volsync.backube/v1alpha1
   kind: ReplicationDestination
   metadata:
     name: home-restore
     namespace: hogwarts-restore
   spec:
     trigger: {manual: restore-<YYYYMMDDTHHMMSSZ>}
     restic:
       repository: restic-config-home
       destinationPVC: home-test
       copyMethod: Direct
       restoreAsOf: "<RFC3339 time of the recorded snapshot>"
   ```

   Done when `lastManualSync` equals the token and
   `latestMoverStatus.result: Successful`; then `spec.paused: true`.

5. **Ownership of `disk.img`.** The mover runs with an arbitrary UID and
   writes `disk.img` as that UID (knx-ets, 2026-10-11); qemu (107) can't open
   it then. From a helper Job (dedicated ServiceAccount, `anyuid` SCC only
   for this namespace, see volsync-restore.md "Helper pods"):
   `chown 107:107 /restore/disk.img` (that one file, not `-R`), then
   `stat -c '%s %u:%g' /restore/disk.img`: owner `107:107`, size equal to the
   production file (`ls -ln` of `disk.img` inside the running hogwarts
   virt-launcher pod, compute container).

6. **Boot test.** In the scratch namespace create:
   - a fresh root disk: copy `components-infra/hogwarts/base/datavolume-rootdisk.yaml`
     (same pinned image), name `hogwarts-restore-rootdisk`, without the Argo
     annotations;
   - the cloud-init ExternalSecret: copy `external-secret-cloudinit.yaml`,
     namespace changed, `hostname: hogwarts-restore`. Its `fs_setup` uses
     `partition: auto`, so it must **reuse** the restored ext4 labelled `home`
     and not format it: this is the check that a root-disk rebuild keeps
     `/home`;
   - both NetworkPolicies (`networkpolicy-egress.yaml`,
     `networkpolicy-ingress.yaml`) with the namespace changed and the pod
     selector `vm.kubevirt.io/name: hogwarts-restore`, and the egress copy
     **without the internet rule** (only DNS and the in-cluster Services stay).
     cloud-init's package step then fails; that is expected and harmless (use
     `virtctl console` or SSH from knowhere instead of the guest agent);
   - the VM: copy `virtualmachine.yaml`, `name: hogwarts-restore`, volumes
     pointing at `hogwarts-restore-rootdisk`, `home-test` (as a
     `persistentVolumeClaim` volume, the PVC is not a DataVolume) and the
     scratch cloud-init Secret, `runStrategy: Halted`; start it with
     `virtctl start hogwarts-restore -n hogwarts-restore`.

   Success criteria (console or SSH from knowhere with the admin key):
   - `findmnt /home` shows the virtio-homedisk device, and `dmesg` / the
     journal shows no `mkfs` and no ext4 errors beyond a journal replay;
   - both homes exist with their pinned owners (`ls -ln /home`: 1000, 1001);
   - `loginctl show-user <julia>` reports no linger and none of Julia's
     `systemctl --user` units runs;
   - Julia's Hermes state is readable: `sqlite3 <home>/.hermes/state.db
     'pragma integrity_check'` (as root, read-only) answers `ok`, and the
     newest session time matches the snapshot time;
   - the canary checks of spec section 10 (canary session resumes, kanban
     card, cron job with `next_run_at`, memory fact, user-installed tool,
     approval record). Before the first `hermes` command, empty the Telegram
     bot token lines in every profile `.env` **of this scratch copy** and
     confirm no gateway process runs afterwards (`pgrep -af hermes`).

7. **Cleanup.** `virtctl stop`, delete the namespace, revert the
   restic-rest-server namespace entry (or unfreeze its app), remove the
   `anyuid` grant, and check that `home-backup` in `hogwarts` still has its
   schedule and no manual trigger. Record the result (snapshot, timings,
   criteria) on the Hogwarts backup issue in infra-ops (spec section 10).

## Part B: restore over the live `/home` (needs Jonas's go)

Use when `/home` is corrupt or must roll back. Plan first: target snapshot,
steps below with exact names, the pre-restore backup, revert path.

1. **Freeze GitOps.** In stage 1 (`enableAutoSync: false` on `hogwarts`) there
   is nothing to freeze. Once auto-sync is on, remove `automated` from the
   parent `cluster-config-manager` and from `hogwarts` and record both
   `syncPolicy`s (volsync-restore.md step 1).
2. **Stop the VM:** `virtctl stop hogwarts -n hogwarts`. ArgoCD ignores
   `/spec/runStrategy`, so it stays `Halted`. Wait until the VMI is gone and
   prove the PVC is detached (volsync-restore.md step 3: no pod, no
   VolumeAttachment for `hogwarts-home`).
3. **Park** `home-backup` (volsync-restore.md "Parking a ReplicationSource").
4. **Revert point before you overwrite anything:** a CSI snapshot of the
   current disk:

   ```yaml
   apiVersion: snapshot.storage.k8s.io/v1
   kind: VolumeSnapshot
   metadata:
     name: hogwarts-home-pre-restore-<ts>
     namespace: hogwarts
   spec:
     volumeSnapshotClassName: lvms-vg1
     source: {persistentVolumeClaimName: hogwarts-home}
   ```

   Wait for `readyToUse: true`. Revert = a new PVC from this snapshot
   (`dataSource` VolumeSnapshot, same size and class) restored back the same
   way, or simply the restic snapshot taken before the incident.
5. **Restore** in place with an RD in `hogwarts` (`destinationPVC:
   hogwarts-home`, `repository: restic-config-home`, `copyMethod: Direct`,
   `restoreAsOf` pinned, manual trigger), wait as in part A step 4, then
   pause or delete the RD **before** the VM starts.
6. **Ownership:** `disk.img` to `107:107` and size check, as in part A
   step 5 (helper Job in `hogwarts`, SCC grant removed afterwards).
7. **Start:** `virtctl start hogwarts -n hogwarts`. Check `findmnt /home`,
   the owners, Julia's Hermes gateway healthy and her bots answering, and
   that Tailscale is `Running`.
8. **Return to service** (volsync-restore.md section 4): fresh manual backup
   from the restored disk, un-park `home-backup` and confirm
   `status.nextSyncTime`, restore the recorded `syncPolicy`s, delete the
   VolumeSnapshot once the restore is accepted.

## Root disk lost or broken

No restore: rebuild. Stop the VM, delete the DataVolume `hogwarts-rootdisk`
by hand (it carries `Prune=false,Delete=false`, so ArgoCD never deletes it),
then sync `hogwarts` (manual sync in stage 1). CDI imports the pinned image
again; cloud-init recreates both accounts with the pinned UIDs and mounts the
existing `/home` (`partition: auto` reuses the ext4 labelled `home`). Then run
the infra-ops Ansible for the hogwarts host from knowhere. Check `/home`
before Ansible runs: if cloud-init had formatted it, stop and go to part B.
