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

The production VM keeps running. Budget about two hours: one for the restore
and boot (60Gi image), the rest for the canary checks, the revert rehearsal
(step 7) and the cleanup.

**Scripted path.** infra-ops `mise run hogwarts:restore-drill` on knowhere
runs the whole drill (spec section 10): canaries on the live host, a fresh
backup, steps 2 to 8 below with the scratch objects from
[components-infra/hogwarts/drill](../../components-infra/hogwarts/drill/README.md)
(not part of any Argo Application), timings per phase, a cleanup from its
EXIT trap, and a result file that `hogwarts:verify` reports. Like
`hogwarts:restart` it runs only on Jonas's word: his own terminal with a
typed confirmation, or Wong's chat session with his go. The steps below are
what the script does, and the manual fallback.

**Not covered by this drill:** a restore from the offsite copy. tailsafe
copies the whole rest-server directory to the NAS-side target; getting the
repo back from there (first the directory, then the restic restore) is a
separate drill that has not been run yet.

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

   `hogwarts-restore` is listed permanently among the mover namespaces of
   `components-infra/restic-rest-server/networkpolicy.yaml` (the namespace
   exists only during a drill); a drill under another namespace name would
   hang on connect until it is added there.

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
     **without the internet rule and without the Nextcloud, Paperless, Qdrant
     and Gitea rules** (only DNS and LiteLLM stay), so a restored agent can't
     write into Julia's live data. Without internet, cloud-init's package
     step (`package_update`, `qemu-guest-agent`) **fails, as expected**:
     `cloud-init status` reports `error` with
     `package-update-upgrade-install` as the only failed module, and the VM
     has no guest agent. Everything else (accounts, `/home` mount, sshd
     config) runs before that step;
   - the VM: copy `virtualmachine.yaml`, `name: hogwarts-restore`, volumes
     pointing at `hogwarts-restore-rootdisk`, `home-test` (as a
     `persistentVolumeClaim` volume, the PVC is not a DataVolume) and the
     scratch cloud-init Secret, `runStrategy: Halted`; start it with
     `virtctl start hogwarts-restore -n hogwarts-restore`.

   Access: SSH from knowhere as the admin account (both passwords are
   locked, so `virtctl console` shows the boot but allows no login). The
   scratch VM generates new host keys; pin one instead of trusting on first
   use: generate a throwaway ed25519 host key on knowhere, add it to the
   scratch cloud-init as `ssh_keys: {ed25519_private: ..., ed25519_public:
   ...}` and connect with only that key in a scratch `known_hosts`. The
   drill script does exactly this (`components-infra/hogwarts/drill/boot`
   renders the template; the script adds the key).

   The guest has **no `sqlite3` CLI**; it has `python3`, `jq` and `git`.
   Read SQLite files with the Python module, as Julia (so no file in her
   home becomes root's), e.g. the integrity check:

   ```sh
   runuser -u <julia> -- python3 -I -c 'import sqlite3, sys
   con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
   print(con.execute("pragma integrity_check").fetchone()[0])' \
     /home/<julia>/.hermes/state.db
   ```

   Success criteria (SSH from knowhere with the admin key):
   - `findmnt /home` shows the virtio-homedisk device, and `dmesg` / the
     journal shows no `mkfs` and no ext4 errors beyond a journal replay;
   - both homes exist with their pinned owners (`ls -ln /home`: 1000, 1001);
   - `loginctl show-user <julia>` reports no linger and none of Julia's
     `systemctl --user` units runs;
   - Julia's Hermes state is readable: `pragma integrity_check` (Python
     module as above, read-only) answers `ok` for the default and every
     profile `state.db` and for `kanban.db`, and the newest session time
     matches the snapshot time;
   - the canary checks of spec section 10 (canary session resumes, kanban
     card, cron job with `next_run_at`, memory fact, user-installed tool,
     approval record).

   **Before the first `hermes` command** (any `hermes` call auto-spawns a
   gateway, and that gateway runs the cron scheduler and the kanban
   dispatcher), in **this scratch copy** only, as root on the scratch VM, in
   this order:
   1. empty the Telegram bot token lines in every profile `.env`;
   2. read the cron canary straight from the files, without Hermes:
      `jq -r '.jobs[] | [.name, .next_run_at, (.repeat|tostring)] | @tsv'
      <home>/.hermes/profiles/*/cron/jobs.json` (and the default profile's
      `<home>/.hermes/cron/jobs.json`), and record it. **Read first, then
      move:** after step 3 neither `jq` nor `hermes cron list` can see it;
   3. pause every cron job by moving each `cron/jobs.json` aside
      (`mv jobs.json jobs.json.drill-paused`), so the spawned gateway finds
      no job to run;
   4. read the kanban canary read-only with the Python module (`select id,
      status, assignee from tasks order by created_at desc limit 5` on
      `<home>/.hermes/kanban.db`, `mode=ro`); note any `ready` card (the
      spawned dispatcher could start it; with only DNS and LiteLLM reachable
      it can't act outside the VM);
   5. move Julia's Claude login aside: `mv <home>/.claude-hogwarts
      <home>/.claude-hogwarts.drill-aside`. The copy holds the same OAuth
      refresh token as the live VM; a refresh from the copy would rotate it
      and log out the live gateway. Without internet a refresh can't reach
      Anthropic anyway, but the directory must not be usable at all;
   6. only now run the `hermes` checks. The default model (Claude through
      the login) is unreachable without internet and its login is moved
      aside, so the canary session runs on the LiteLLM fallback: `hermes -p
      hermione chat -Q --resume <canary session id> --provider
      custom:litellm -m <assistant_agents_fallback_model> -q "..."`. Confirm
      afterwards that no gateway keeps running (`pgrep -af hermes`).

7. **Revert-path rehearsal (VolumeSnapshot).** Part B's revert point is
   untested until this step has passed once; record the result. With
   `hogwarts-restore` stopped (`virtctl stop`, VMI gone):
   - VolumeSnapshot `home-test-snap` of `home-test` (class `lvms-vg1`), wait
     for `readyToUse: true`;
   - start the VM, write a marker that must NOT survive the revert (as root:
     `date -u > /home/.drill-revert-marker`), stop it again; without such a
     change the copy back would prove nothing;
   - PVC `home-test-revert` from it (60Gi, `lvms-vg1`, `ReadWriteOnce`,
     Filesystem, `dataSource: {apiGroup: snapshot.storage.k8s.io, kind:
     VolumeSnapshot, name: home-test-snap}`);
   - copy the content back with the copy Job of part B step 4, run in
     `hogwarts-restore` with the helper ServiceAccount of step 5 (source
     `home-test-revert`, destination `home-test`);
   - start the VM again: `findmnt /home` on the home disk, `ls -ln /home`
     owners 1000/1001, `pragma integrity_check` `ok`, and the marker is
     gone.

8. **Cleanup.** `virtctl stop`, delete the namespace (the restored copy,
   its Secrets and the `anyuid` RoleBinding go with it), and check that
   `home-backup` in `hogwarts` still has its schedule and no manual trigger.
   Remove the canaries from the live host (card, memory entry, tool,
   approval entry, canary session). Record the result (snapshot, timings,
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

   Wait for `readyToUse: true`. An lvms snapshot lives in the same thin pool
   as the disk: it protects against a bad restore, not against losing the
   pool (the restic snapshots stay the real backup).

   **Revert** (untested until part A step 7 has passed once):
   1. VM stopped, RD paused or deleted.
   2. PVC `hogwarts-home-revert` from the snapshot:

      ```yaml
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: hogwarts-home-revert
        namespace: hogwarts
      spec:
        storageClassName: lvms-vg1
        accessModes: [ReadWriteOnce]
        volumeMode: Filesystem
        resources: {requests: {storage: 60Gi}}
        dataSource:
          apiGroup: snapshot.storage.k8s.io
          kind: VolumeSnapshot
          name: hogwarts-home-pre-restore-<ts>
      ```

   3. **Never delete or swap `hogwarts-home`.** It belongs to the DataVolume
      `hogwarts-home` (CDI): a deleted PVC comes back blank on the next sync,
      and pointing the VM at another claim is a Git change. Copy the content
      back instead, with a helper Job in `hogwarts` (same ServiceAccount and
      temporary `anyuid` grant as step 6) that mounts `hogwarts-home-revert`
      read-only at `/src` and `hogwarts-home` at `/dst` and runs
      `cp --sparse=always /src/disk.img /dst/disk.img && chown 107:107
      /dst/disk.img && stat -c '%s %u:%g' /dst/disk.img`. Interrupted: run it
      again (the snapshot is untouched).
   4. Start and check as in step 7; once accepted delete
      `hogwarts-home-revert` and the VolumeSnapshot.

   Alternatively restore the restic snapshot taken before the incident
   (step 5 with that `restoreAsOf`).
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
then sync `hogwarts` (manual sync in stage 1). The VM is still `Halted`, so
the new DataVolume has no consumer yet and waits in `PendingPopulation`, and
the sync operation stays `Running` in wave 1 (see
[sync order](#sync-order-first-sync-and-rebuilds)). Start the VM
(`virtctl start hogwarts -n hogwarts`): CDI imports the pinned image again,
the VM boots and the sync finishes. cloud-init recreates both accounts with
the pinned UIDs and mounts the existing `/home` (`partition: auto` reuses the
ext4 labelled `home`). Then run the infra-ops Ansible for the hogwarts host
from knowhere. Before Ansible runs, do the
[first-boot checks](#first-boot-checks-first-sync-and-every-root-disk-rebuild):
if cloud-init had formatted `/home`, stop and go to part B.

## Sync order (first sync and rebuilds)

Both disks are on `lvms-vg1`, a `WaitForFirstConsumer` StorageClass: a new
DataVolume stays in `PendingPopulation` (no PVC bound, no import) until a pod
uses its PVC, and the only such pod is the VM's virt-launcher. ArgoCD's
built-in DataVolume health check reports that phase as `Progressing`, and a
sync waits for every resource of a wave to be healthy before the next wave.
So the DataVolumes sit in the same wave as the VM:

| Wave | Objects |
|---|---|
| -1 | Namespace |
| 0 | cloud-init ExternalSecret, NetworkPolicies, Service |
| 1 | both DataVolumes and the VirtualMachine |
| 30, 200 | restic ExternalSecret, ReplicationSource `home-backup` (VolSync template) |

A first sync (or a sync after both disks were lost) applies wave 1 at once:
the VM binds both PVCs, CDI imports the root image and creates the blank
home disk, the VM boots, and once DataVolumes and VM are healthy the sync
goes on to the backup objects (so `home-backup` never exists before the
disk). With the DataVolumes in an earlier wave than the VM, the first sync
hung for good in that wave and applied neither the VM nor the backup
objects (2026-11-08).

A sync that waits on a DataVolume in `PendingPopulation` while the VM is
`Halted` (root-disk rebuild above) is not stuck: start the VM. Never sync
with Replace or Force to get past it.

## First-boot checks (first sync and every root-disk rebuild)

Run before the first Ansible run against the VM. From knowhere, SSH in over
the pod network with the admin key (`ssh <admin>@hogwarts-ssh.hogwarts.svc`);
`<admin>` and `<julia>` are the two account names from Doppler. Every line
must show the expected result; on any mismatch stop the VM
(`virtctl stop hogwarts -n hogwarts`) and run no Ansible.

| Check | Command | Expected |
|---|---|---|
| VM up, guest agent | `oc get vmi hogwarts -n hogwarts -o jsonpath='{.status.phase} {.status.conditions[?(@.type=="AgentConnected")].status}'` | `Running True` |
| cloud-init finished | `cloud-init status --long` | `status: done`, no errors |
| exactly the two accounts | `getent passwd 1000 1001 \| cut -d: -f1,3,6` | `<admin>:1000:/home/<admin>` and `<julia>:1001:/home/<julia>`, nothing else |
| no image default user | `id ubuntu` | `no such user` |
| `/home` on the home disk | `[ "$(findmnt -no SOURCE /home)" = "$(readlink -f /dev/disk/by-id/virtio-homedisk)" ] && echo ok` and `sudo blkid -s LABEL -o value /dev/disk/by-id/virtio-homedisk` | `ok`, `home` |
| home owners | `stat -c '%u:%g %n' /home/*` | `1000:1000 /home/<admin>`, `1001:1001 /home/<julia>` |
| Julia without sudo | `sudo -l -U <julia>` and `id -nG <julia>` | "not allowed to run sudo"; only her own group |
| passwords locked | `sudo passwd -S <admin> <julia>` | second field `L` on both lines |
| no keys for Julia | `sudo test -e /home/<julia>/.ssh/authorized_keys && echo FOUND \|\| echo none` | `none` |
| sshd admits only the admin | `sudo sshd -T \| grep -E '^(allowusers\|passwordauthentication) '` | `allowusers <admin>`, `passwordauthentication no` |

UIDs taken or the `ubuntu` user present: the pinned image changed behaviour;
fix cloud-init, delete the root disk DataVolume and sync again. `/home` not on
the home disk: the VM must not keep running (Julia's state would land on the
disposable root disk).
