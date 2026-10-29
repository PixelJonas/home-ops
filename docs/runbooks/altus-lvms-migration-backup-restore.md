# Altus LVMS migration: backup and restore runbook

Date of read-only inventory: 2026-10-26

This is the backup/restore companion to the Altus LVMS rebuild and Knowhere
isolation plan. It covers every PVC that used `lvms-vg1` at the inventory
checkpoint. Knowhere is the exception to the offline workflow: its two active
disks move together in two live storage-migration legs while the same running VM
moves Altus → xandar → Altus. Knowhere must remain running and available for the
entire process. Direct/same-node migration from local LVMS storage is impossible.

The old pool must not be recreated until the first Knowhere leg and disposable
B18a rehearsal plus their checks complete, and every other preserved production
row has a fresh, verified backup and required restore test. After the rebuild
creates `lvms-knowhere`, B18b must pass before the production return leg. V03 and
O01 remain backup/disaster-recovery IDs only; neither is a production restore
route.

**Preparation status:** no production backup jobs, restores, workload stops,
deletions, or live storage migrations were run. Synthetic scratch helper tests
were executed and cleaned up without touching production PVCs. The commands
below are instructions for separately approved maintenance and test windows,
not a record that those actions have happened. Gate B and Gate C evidence is
still pending. The synthetic qcow2/GPG and libguestfs helper evidence below
passed; B18 has not passed and remains pending.

## Non-negotiable safety rules

- Run final migration operations from outside Knowhere, but do not stop Knowhere,
  its guest, its agent sessions, or its services at any point. Continuous
  availability is a requirement, not a best-effort target.
- Pause ArgoCD auto-sync on affected parents first and children second. Record
  every changed Application. Otherwise a scale-down, PVC, DataVolume, or helper
  can be reconciled away or recreated unexpectedly. Freeze Knowhere GitOps around
  each volume-reference change; reconcile the VM, DataVolume/PVC and VolSync
  references deliberately before unfreezing.
- Never run a final backup against an active writer. Except for M01/M02, stop
  applications before their databases, halt KNX, and verify source volumes are
  detached before an offline copy. The only other exception is the logical
  database dump itself, taken while its database is available and all application
  writers are stopped. Do not halt Knowhere for a backup, copy, test, or cutover.
- Each Knowhere `VirtualMachineStorageMigrationPlan` must include both active
  disks and set `spec.retentionPolicy: keepSource`. No migration step may
  automatically delete a source PVC/PV/DataVolume. A failed leg stops all rebuild
  work. Cancellation is valid only while a migration is in progress; cancelling
  or deleting resources after `Completed` is not rollback.
- Run data movement sequentially, or at most two streams with explicit approval.
  Stop on sustained Altus root-disk utilization above 80%, host iowait above
  20%, or abnormal etcd WAL latency.
- The approved NAS/restic targets are independent of `vg1`. The offline-artifact
  destination has 25.52TiB free, against a conservative current requirement of
  296.645GiB, 370.807GiB with 25% margin, and a worst-case full-root budget of
  604.357GiB. Reconcile allocated-data measurements again before the final copy.
- Do not print, export, or copy Secret contents. Use the existing
  `ExternalSecret` resources and `doppler-cluster` ClusterSecretStore. Never use
  `oc get secret ... -o yaml`, `oc extract`, or decoded Secret output for this
  migration.
- Plaintext O01–O07 artifacts are prohibited. Optional O01 DR artifacts use
  qcow2-LUKS; O02–O07 stream tar through GnuPG AES256 so plaintext never lands on
  NFS. Use separate Doppler-owned high-entropy passphrases, each mounted as a
  0400 Secret file.
- Do not expose Knowhere's production root block device to a second VM or helper
  during this live migration. Any future optional O01 DR helper requires separate
  approval and must not become a production migration mechanism. Restore tests
  use a new block PVC and no production VM network identity.
- Numeric ownership is part of the data. Capture it before stopping workloads;
  do not recursively `chown` an entire application volume to a guessed UID.
  Known exceptions are KubeVirt's `disk.img`/swtpm files (`107:107`) and the
  mover-specific contexts listed below.
- Restic `lastSyncTime`, a successful dump Job, or a Ready pod alone is not
  restore evidence. Require a fresh artifact, nonzero size, checksum/repository
  check, and a demonstrated restore.
- Local restic repositories use direct NFS on `/volume1/configs`, independent of
  `vg1`, and already have tailsafe protection. The new migration-artifact
  directory does not; Gate A requires either protection or explicit risk
  acceptance as described below.

## Approved Gate A destination and encryption material

The approved destination is `10.10.10.40:/volume1/pmb-backup`. All O01–O07
helpers mount only the dedicated `/volume1/pmb-backup/altus-lvms-migration/`
directory; do not write artifacts into the export root or `/volume1/configs`.
The export uses `all_squash`, `anonuid=1024`, and `anongid=100`, so the dedicated
directory must be writable by that identity. NFS-visible file ownership is
therefore not application ownership: numeric application owners, modes, ACLs,
xattrs, and SELinux labels remain inside the encrypted tar streams.

Capacity is approved at the inventory checkpoint:

| Measurement | Budget |
|---|---:|
| NFS free space | 25.52TiB |
| Conservative current allocated-data need | 296.645GiB |
| Current need plus 25% margin | 370.807GiB |
| Worst case with the 300Gi root treated as fully allocated | 604.357GiB |

The migration directory is **not currently tailsafe-covered**. Before Gate A is
signed, either add and verify equivalent independent protection for this
directory, or record explicit operator acceptance that these encrypted migration
artifacts have only their NAS-resident copy during the migration/retention
window. Existing restic tailsafe protection on `/volume1/configs` does not cover
this new directory and must not be cited as evidence that it does.

**Operator decision, 2026-10-27:** the operator explicitly accepted the
NAS-only risk for this migration/retention window. The migration directory will
not receive independent tailsafe protection under this acceptance; encrypted
artifacts may therefore have only their NAS-resident copy. This is a recorded
risk acceptance, not evidence of independent protection.

Create the Doppler-owned, high-entropy filesystem passphrase for O02–O07. Create
the independent root passphrase for the mandatory running-production root
safety copy and for optional O01 DR work if separately approved. These are key
names only; never place either value in this file, a Pod argument, shell
environment dump, or the NFS evidence directory:

- `ALTUS_LVMS_MIGRATION_KNOWHERE_ROOT_LUKS_PASSPHRASE`
- `ALTUS_LVMS_MIGRATION_FILESYSTEM_GPG_PASSPHRASE`

On 2026-10-27 both values were created in **Doppler `homelab/home`**, which is
the project visible to the cluster's `doppler-cluster` store. Initial duplicate
entries in `infra-ops/prd` were created during project discovery and are retained
for a separate deletion review; they are not the ESO source of truth.

Materialize required values with ExternalSecrets; do not copy existing Secret
data. The Knowhere example targets the running-root safety secret (also used
for optional O01 DR), and the filesystem example is repeated in each O02–O07
namespace with target name
`altus-lvms-filesystem-encryption`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: altus-lvms-knowhere-root-encryption
  namespace: knowhere
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: altus-lvms-knowhere-root-encryption
  data:
    - secretKey: passphrase
      remoteRef:
        key: ALTUS_LVMS_MIGRATION_KNOWHERE_ROOT_LUKS_PASSPHRASE
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: altus-lvms-filesystem-encryption
  namespace: openshift-monitoring
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: altus-lvms-filesystem-encryption
  data:
    - secretKey: passphrase
      remoteRef:
        key: ALTUS_LVMS_MIGRATION_FILESYSTEM_GPG_PASSPHRASE
```

Verify only `SecretSynced` and the mounted file metadata. The root secret mounts
at `/run/secrets/migration/root-luks.passphrase`; the filesystem secret mounts at
`/run/secrets/migration/filesystem-gpg.passphrase`. Both Secret volumes use
`defaultMode: 0400` and a read-only mount. Loss of either Doppler value makes its
artifacts unrestorable; confirm ownership/recovery responsibility before backup.

The verified copy-tool image for all O01–O07 helpers is
`registry.redhat.io/container-native-virtualization/virt-launcher-rhel9@sha256:2131d3d24616971b92d7e986635673b6dc731da3d43cae46540882993d36596f`.
It provides qemu-img 10.1, GNU tar, and GnuPG. It does **not** contain guestfish.
Read-only guest inspection uses the operator-matched, amd64-verified image
`registry.redhat.io/container-native-virtualization/libguestfs-tools-rhel9@sha256:de17d73c4ce0f55a360e60ff9f477abab69dbe8765b0ce3e4339a3ed8dc14ce8`.
Attach the scratch Block PVC with `volumeDevices`; use `guestfish --ro
--format=raw`, with no `/dev/kvm`, hostPath, or privileged SCC. Set
`LIBGUESTFS_BACKEND=direct`,
`LIBGUESTFS_PATH=/usr/local/lib/guestfs/appliance`,
`LIBGUESTFS_BACKEND_SETTINGS=force_tcg`, and `LIBGUESTFS_TMPDIR`,
`LIBGUESTFS_CACHEDIR`, `TMPDIR`, and `HOME` to `/tmp`. Mount writable emptyDirs
at both `/tmp` and `/var/tmp`; QEMU otherwise fails to create its read-only
snapshot overlay. Kubernetes does not enforce read-only raw `volumeDevices`, so
only `guestfish --ro`/`mount-ro` commands are permitted.

On 2026-10-26, synthetic 1Gi tests in the isolated `altus-lvms-helper-test`
namespace passed qcow2-LUKS conversion/check, full source and restored-target
comparisons, encrypted tar creation/decryption/extraction, numeric ownership and
sparse-file checks, and NFS checksum readback. The libguestfs helper also passed
ext4 creation, partition/filesystem discovery, `mount-ro` known-file reads, a
rejected write, and proof that no file was created. Both used only a namespaced
`anyuid` binding, no `privileged` SCC, `allowPrivilegeEscalation: false`, and no
added capabilities for qemu/libguestfs; the filesystem tar helper additionally
required `CHOWN`/`DAC_OVERRIDE`/`FOWNER` after dropping all other capabilities.
GnuPG required a writable 0700 `GNUPGHOME` under `/tmp`. These tests validate the
helper commands and approved NFS path only; they are not Knowhere B18 evidence
and used no production PVC or Secret.

## Exact 104-PVC routing inventory

The production route IDs are disjoint and complete: **M01–M02 = two live
Knowhere storage migrations, V01/V02/V04–V15 = 14 VolSync restores, D01–D11 =
11 logical database dump/imports, O02–O07 = six offline copies, R01–R65 = 65
recreations, and I01–I06 = six managed image reimports**. The exact total is
**M2 + V14 + D11 + O6 + R65 + I6 = 104**. V03 (`knowhere-projects`) and O01
(`knowhere-rootdisk`) remain backup/disaster-recovery and DR-test IDs, but M02
and M01 are their only production migration routes.

Sizes are the live requests, not allocated data. Byte-valued requests are kept
exact where useful for target validation. Immediately before maintenance,
regenerate the live inventory and stop on any added, removed, resized, or
reclassified PVC.

```bash
oc-sa altus get pvc -A -o json | jq -r '
  .items[]
  | select(.spec.storageClassName == "lvms-vg1")
  | [.metadata.namespace, .metadata.name, .spec.volumeMode,
     .spec.resources.requests.storage, .spec.volumeName]
  | @tsv' | sort

oc-sa altus get pvc -A -o json |
  jq '[.items[] | select(.spec.storageClassName == "lvms-vg1")] | length'
# Expected at this checkpoint: 104. Do not proceed on count alone; reconcile names.
```

### M: two live Knowhere disk migrations

| ID | Namespace | Initial source PVC | Request / mode | Production route |
|---|---|---|---:|---|
| M01 | knowhere | knowhere-rootdisk | 322122547200 (300Gi), Block | With M02, live `lvms-vg1` → `synology-iscsi-storage` on Altus → xandar, then live iSCSI → `lvms-knowhere` on xandar → Altus. |
| M02 | knowhere | knowhere-projects | 136579960013 (~127.19Gi), Filesystem | With M01 in both plans; never split the disks across plans or stop the VM. Update VolSync to the active projects PVC after each leg. |

### V: 14 production VolSync filesystem restores plus V03 DR coverage

Restore each production V source with `copyMethod: Direct` into a new, unmounted
destination PVC. “Local+B2” means run and verify both final backups; B2 is the
preferred disaster-recovery source, while either verified repository may be used
for this pool rebuild. V03 is retained below only as DR backup/test coverage; its
restore must never replace M02 in the production route.

| ID | Namespace | Source PVC | Request | ReplicationSource / repository | Restore and ownership notes |
|---|---|---|---:|---|---|
| V01 | actual-budget | actual-budget-app-data | 5Gi | data-backup / restic-config-data; data-backup-b2 / restic-config-data-b2 | Local+B2; preserve application ownership, expected pod UID/GID/fsGroup 1001. |
| V02 | gitea | gitea-shared-storage | 20Gi | data-backup / restic-config-data | Local; coordinated with D01. Preserve existing numeric ownership; do not force 1000. |
| V03 | knowhere | knowhere-projects | 136579960013 (~127.19Gi) | projects-backup / restic-config-projects | **DR test only, not production migration:** isolated restore; exact `/disk.img` path, then `chown 107:107 /disk.img`. |
| V04 | knx-ets | persistent-state-for-windows-11-virtio-ets | 300Mi | persistent-state-backup / restic-config-persistent-state | Local; coordinated with V05; swtpm/NVRAM state; `chown -R 107:107`. |
| V05 | knx-ets | windows-11-virtio-ets-volume | 64Gi | volume-backup / restic-config-volume | Local; coordinated with V04; exact `/disk.img`; `chown -R 107:107`. |
| V06 | media-vpn | qbittorrent-config | 5Gi | qbittorrent-backup / restic-config-qbittorrent; qbittorrent-backup-b2 / restic-config-qbittorrent-b2 | Local+B2; preserve numeric ownership and current fsGroup behavior. |
| V07 | nextcloud | nextcloud-app-nextcloud | 20Gi | data-backup / restic-config-data; data-backup-b2 / restic-config-data-b2 | Local+B2; coordinated with D06 and the separate NFS user-data backup. |
| V08 | paperless | paperless-ng-data | 3Gi | data-backup / restic-config-data; data-backup-b2 / restic-config-data-b2 | Local+B2; coordinated with V09 and D07. |
| V09 | paperless | paperless-ng-media | 15Gi | media-backup / restic-config-media; media-backup-b2 / restic-config-media-b2 | Local+B2; coordinated with V08 and D07. |
| V10 | qdrant | cosmere-knowledge-index-progress | 1Gi | crawl-progress-backup / restic-config-crawl-progress | Local; preserve crawl progress with V11/V12. |
| V11 | qdrant | qdrant-app-data | 10Gi | data-backup / restic-config-data | Local; preserve collection data; pod fsGroup 1001090000 at this checkpoint. |
| V12 | qdrant | qdrant-app-snapshots | 5Gi | snapshots-backup / restic-config-snapshots | Local; restore snapshots as well as live data. |
| V13 | taxbuddy-hermes | taxbuddy-hermes-app | 5Gi | data-backup / restic-config-data | Local; mover must use runAsUser/runAsGroup 10000 and runAsNonRoot without fsGroup. |
| V14 | tymeslot | tymeslot-app | 5Gi | data-backup / restic-config-data; data-backup-b2 / restic-config-data-b2 | Local+B2; coordinated with D09; app runs as root at this checkpoint. |
| V15 | vaultwarden | vaultwarden-app-config | 1Gi | config-backup / restic-config-config | Local; coordinated with D10. |

### D: 11 logical database dump/import PVCs

These are database data PVCs on `lvms-vg1`; their `database-syno-db-backup`
staging PVCs are not on this storage class and therefore are not extra rows in
the 104-PVC inventory. Recreate each data PVC empty, restore the staging dump
with VolSync Direct, initialize only the database, import with no application
writers, and validate before application startup.

Jellystat is deliberately not a D row: its current `jellystat-db-2` data PVC is
not `lvms-vg1`. Its dump schedule should still run normally, and its one
`lvms-vg1` VolSync cache is R09.

| ID | Namespace | Database data PVC | Request | Database / final dump | Dump repositories |
|---|---|---|---:|---|---|
| D01 | gitea | gitea-db-1 | 10Gi | CNPG gitea-db; database-psql-backup; database/owner gitea | restic-config-database |
| D02 | honcho | honcho-db-1 | 5Gi | CNPG honcho-db; database-psql-backup; database/owner honcho | restic-config-database |
| D03 | immich | immich-app | 20Gi | Deployment immich-app-database; database-psql-backup; database immich | restic-config-database and restic-config-database-b2 |
| D04 | ingestbuddy | ingestbuddy-db-1 | 20Gi | CNPG ingestbuddy-db; database-psql-backup; database/owner ingestbuddy | restic-config-database |
| D05 | litellm | litellm-db-1 | 8Gi | CNPG litellm-db; database-psql-backup; database/owner litellm | restic-config-database |
| D06 | nextcloud | data-nextcloud-app-mariadb-0 | 10Gi | StatefulSet nextcloud-app-mariadb; database-mariadb-backup; database nextcloud | restic-config-database and restic-config-database-b2 |
| D07 | paperless | paperless-db-1 | 20Gi | CNPG paperless-db; database-psql-backup; database/owner paperless | restic-config-database and restic-config-database-b2 |
| D08 | taxbuddy | taxbuddy-db-1 | 10Gi | CNPG taxbuddy-db; database-psql-backup; database/owner taxbuddy | restic-config-database |
| D09 | tymeslot | tymeslot-db-1 | 2Gi | CNPG tymeslot-db; database-psql-backup; database/owner tymeslot | restic-config-database and restic-config-database-b2 |
| D10 | vaultwarden | vaultwarden-db-1 | 2Gi | CNPG vaultwarden-db; database-psql-backup; database/owner vaultwarden | restic-config-database and restic-config-database-b2 |
| D11 | vehicle-pipeline | vehicle-pipeline-db-1 | 5Gi | CNPG vehicle-pipeline-db; database-psql-backup; database/owner vehicle_pipeline | restic-config-database |

Authoritative application coordination: Gitea V02+D01; Immich D03 plus its NFS
library backup; Nextcloud V07+D06 plus its separate NFS user-data backup;
Paperless V08+V09+D07; Taxbuddy D08 plus its non-persistent queue dependencies;
Tymeslot V14+D09; Vaultwarden V15+D10.

### O: six production offline copies plus optional O01 DR coverage

O01 is **not** part of this production migration or its mandatory test gates. It
may remain only as an optional, separately approved disaster-recovery backup;
M01 is the sole production root-disk route. Do not halt production Knowhere to
create O01 or to rehearse this migration.

| ID | Namespace | Source PVC | Request / mode | Offline artifact and purpose |
|---|---|---|---|---|
| O01 | knowhere | knowhere-rootdisk | 322122547200 (300Gi), Block | **Optional DR only:** sparse qcow2-LUKS on approved NFS; isolated DR restore test, never production migration. |
| O02 | openshift-image-registry | image-registry | 150Gi, Filesystem | GnuPG-AES256-encrypted numeric-owner/xattr/ACL/SELinux/sparse tar stream; preserve complete registry content. |
| O03 | openshift-monitoring | alertmanager-main-db-alertmanager-main-0 | 10Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve alerts, silences, and notification state. |
| O04 | openshift-monitoring | prometheus-k8s-db-prometheus-k8s-0 | 40Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve all platform Prometheus history; do not trim. |
| O05 | openshift-user-workload-monitoring | prometheus-user-workload-db-prometheus-user-workload-0 | 40Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve all UWM history; do not trim. |
| O06 | openshift-user-workload-monitoring | thanos-ruler-user-workload-data-thanos-ruler-user-workload-0 | 40Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve rule state and local data. |
| O07 | paperless-mcp | paperless-mcp-app | 1Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve OAuth DCR clients, signing keys, and proxy sessions/state. |

### R: 65 recreations without data restore

Do not copy these claims. Recreate them from GitOps after the replacement
StorageClasses are Ready. The 48 VolSync cache claims are disposable mover
caches; use the intended destination cache class. Knowhere's cache moves off
`lvms-vg1` for the interim iSCSI leg and finally to `lvms-knowhere`; audit that
no other application's final cache lands on `lvms-knowhere`.

| ID | Namespace | PVC | Request | Recreation reason |
|---|---|---|---:|---|
| R01 | actual-budget | volsync-src-data-backup-b2-cache | 1Gi | VolSync cache |
| R02 | actual-budget | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R03 | gitea | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R04 | gitea | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R05 | honcho | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R06 | immich | volsync-src-database-backup-b2-cache | 1Gi | VolSync cache |
| R07 | immich | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R08 | ingestbuddy | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R09 | jellystat | volsync-src-database-backup-cache | 1Gi | VolSync cache; database itself is outside lvms-vg1 |
| R10 | knowhere | volsync-src-projects-backup-cache | 1Gi | VolSync cache; interim off `lvms-vg1`, final `lvms-knowhere` |
| R11 | knx-ets | volsync-src-persistent-state-backup-cache | 1Gi | VolSync cache |
| R12 | knx-ets | volsync-src-volume-backup-cache | 1Gi | VolSync cache |
| R13 | litellm | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R14 | media | volsync-src-audiobookshelf-backup-cache | 1Gi | VolSync cache |
| R15 | media | volsync-src-overseerr-backup-cache | 1Gi | VolSync cache |
| R16 | media | volsync-src-prowlarr-backup-cache | 1Gi | VolSync cache |
| R17 | media | volsync-src-radarr-backup-cache | 1Gi | VolSync cache |
| R18 | media | volsync-src-readarr-backup-cache | 1Gi | VolSync cache |
| R19 | media | volsync-src-sonarr-anime-backup-cache | 1Gi | VolSync cache |
| R20 | media | volsync-src-sonarr-backup-cache | 1Gi | VolSync cache |
| R21 | media-vpn | volsync-src-qbittorrent-backup-b2-cache | 1Gi | VolSync cache |
| R22 | media-vpn | volsync-src-qbittorrent-backup-cache | 1Gi | VolSync cache |
| R23 | nextcloud | volsync-src-data-backup-b2-cache | 1Gi | VolSync cache |
| R24 | nextcloud | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R25 | nextcloud | volsync-src-data-nfs-backup-cache | 1Gi | VolSync cache for non-pool NFS source |
| R26 | nextcloud | volsync-src-database-backup-b2-cache | 1Gi | VolSync cache |
| R27 | nextcloud | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R28 | paperless | volsync-src-data-backup-b2-cache | 1Gi | VolSync cache |
| R29 | paperless | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R30 | paperless | volsync-src-database-backup-b2-cache | 1Gi | VolSync cache |
| R31 | paperless | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R32 | paperless | volsync-src-export-backup-b2-cache | 1Gi | VolSync cache for non-pool export source |
| R33 | paperless | volsync-src-export-backup-cache | 1Gi | VolSync cache for non-pool export source |
| R34 | paperless | volsync-src-media-backup-b2-cache | 1Gi | VolSync cache |
| R35 | paperless | volsync-src-media-backup-cache | 1Gi | VolSync cache |
| R36 | qdrant | volsync-src-crawl-progress-backup-cache | 1Gi | VolSync cache |
| R37 | qdrant | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R38 | qdrant | volsync-src-snapshots-backup-cache | 1Gi | VolSync cache |
| R39 | taxbuddy | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R40 | taxbuddy-hermes | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R41 | tymeslot | volsync-src-data-backup-b2-cache | 1Gi | VolSync cache |
| R42 | tymeslot | volsync-src-data-backup-cache | 1Gi | VolSync cache |
| R43 | tymeslot | volsync-src-database-backup-b2-cache | 1Gi | VolSync cache |
| R44 | tymeslot | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R45 | vaultwarden | volsync-src-config-backup-cache | 1Gi | VolSync cache |
| R46 | vaultwarden | volsync-src-database-backup-b2-cache | 1Gi | VolSync cache |
| R47 | vaultwarden | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R48 | vehicle-pipeline | volsync-src-database-backup-cache | 1Gi | VolSync cache |
| R49 | gitea | data-runner-gitea-actions-app-runner-0 | 5Gi | Disposable runner state |
| R50 | gitea | data-runner-gitea-actions-app-runner-1 | 5Gi | Disposable runner state |
| R51 | gitea | data-runner-gitea-actions-app-runner-2 | 5Gi | Disposable runner state |
| R52 | gitea | gitea-actions-app-runner-0-dind-storage | 250Gi | Recreate empty DinD storage; do not copy container layers |
| R53 | gitea | gitea-actions-app-runner-0-runner-cache | 10Gi | Disposable runner cache |
| R54 | gitea | gitea-actions-app-runner-1-dind-storage | 250Gi | Recreate empty DinD storage; do not copy container layers |
| R55 | gitea | gitea-actions-app-runner-1-runner-cache | 10Gi | Disposable runner cache |
| R56 | gitea | gitea-actions-app-runner-2-dind-storage | 250Gi | Recreate empty DinD storage; do not copy container layers |
| R57 | gitea | gitea-actions-app-runner-2-runner-cache | 10Gi | Disposable runner cache |
| R58 | nextcloud | redis-data-nextcloud-app-redis-master-0 | 2Gi | Recreate Redis state |
| R59 | nextcloud | redis-data-nextcloud-app-redis-replicas-0 | 2Gi | Recreate Redis state |
| R60 | nextcloud | redis-data-nextcloud-app-redis-replicas-1 | 2Gi | Recreate Redis state |
| R61 | nextcloud | redis-data-nextcloud-app-redis-replicas-2 | 2Gi | Recreate Redis state |
| R62 | docling-serve | docling-serve-app | 5Gi | Recreate Hugging Face cache |
| R63 | honcho | honcho-app | 10Gi | Recreate model/runtime cache; persistent data is D02 |
| R64 | ingestbuddy | ingestbuddy-app | 1Gi | Recreate runtime cache; persistent data is D04 |
| R65 | taxbuddy | taxbuddy-app | 1Gi | Recreate runtime cache; persistent data is D08 |

### I: six DataImportCron reimports

These are managed OS images, not backups of guest data. Let the existing
DataImportCrons/DataSources create fresh Block PVCs on `lvms-vg1`; do not copy
old image PVCs and do not launch duplicate manual imports.

| ID | Current PVC | Request | Managing DataImportCron | Managed DataSource |
|---|---|---:|---|---|
| I01 | centos-stream10-09e760b90d13 | 32212254720 (30Gi) | centos-stream10-image-cron | centos-stream10 |
| I02 | centos-stream9-1c11524669d7 | 32212254720 (30Gi) | centos-stream9-image-cron | centos-stream9 |
| I03 | fedora-1217dcc8c58d | 32212254720 (30Gi) | fedora-image-cron | fedora |
| I04 | rhel10-c5b97492a6e3 | 32212254720 (30Gi) | rhel10-image-cron | rhel10 |
| I05 | rhel8-c6a08edc555b | 32212254720 (30Gi) | rhel8-image-cron | rhel8 |
| I06 | rhel9-7005186c23b8 | 32212254720 (30Gi) | rhel9-image-cron | rhel9 |

All six objects are in `openshift-virtualization-os-images`. The current cron
schedule is `29 6/12 * * *`; a hash-suffixed PVC name will change on reimport.
Validate the DataSource's current ready PVC rather than expecting the old name.

## Evidence and preflight record

Create a protected, off-node evidence directory before maintenance. Record only
non-secret metadata. Suggested files are `inventory-before.tsv`,
`owners-before/`, `backup-evidence.tsv`, `restore-evidence.tsv`, `db-counts-before/`,
and `db-counts-after/`. Do not put Secret YAML, kubeconfigs, SQL dumps, or restic
credentials in this directory.

Capture ownership while volumes are mounted, using a read-only helper where
necessary:

```bash
# Inside the helper; /src is the source mount. Numeric IDs, modes, sparse
# allocation, and SELinux labels are evidence, not values to guess later.
find /src -xdev -printf '%P\t%U:%G\t%m\t%y\t%s\n' | sort > /backup/owners-before.txt
find /src -xdev -type f -printf '%P\t%s\t%b\n' | sort > /backup/files-before.txt
stat -c '%n %s bytes %b blocks %u:%g %a' /src/disk.img 2>/dev/null || true
ls -Zd /src /src/disk.img 2>/dev/null || true
```

For large data sets, do not hash every file repeatedly during the final outage.
Create a representative checksum manifest before the outage and a full file
count/size manifest at final backup. Hash all small configuration/state volumes
and each complete offline archive. Hash optional O01 only under its separate DR
approval; live M01/M02 use migration and continuous-I/O evidence instead.

### Gate A execution checkpoint: 2026-10-27

The non-secret inventory/metadata baseline was captured and reconciled by name
against all 104 documented route IDs: 104 live `lvms-vg1` PVCs, all Bound, with
zero missing or unexpected names. Knowhere remained Running/Ready on Altus with
`StorageLiveMigratable=True` and `BlockMigration`; the migration API/controller
checks passed. The NAS `/volume1` reported 28,045,274,341,376 bytes available
(about 26 TiB), and the dedicated migration directory passed a
write/readback/cleanup probe. TopoLVM reported 2,739.088 GiB physical thin-pool
free, so LVMS has headroom for its half of B18; this is not a substitute for
per-batch allocated-data checks.

The metadata archive `altus-gate-a-inventory-20261027.tar.gz` was scanned with
gitleaks, uploaded to the approved migration directory, and verified with
SHA-256 `102c3955970db7659de2bc08d949eb202514bbf4e2e40e0b99200a70c729ed92`.
All 52 ReplicationSources reported `Successful`, including Knowhere's
`projects-backup`; this is a baseline only, not restore evidence. The Knowhere
and nmstate ArgoCD Applications remain Healthy/Synced with automated
self-heal/prune enabled, so their deliberate freeze is still pending.

Both migration passphrases were created in Doppler `homelab/home`; all five
ExternalSecrets reached `Ready/SecretSynced`, and temporary helper pods verified
nonempty mounted files with mode `0400` (using `stat -L` for the Secret symlink).
The pods were removed; only the namespaced helper ServiceAccounts/RoleBindings
remain for later tests. The operator explicitly accepted NAS-only artifact risk
on 2026-10-27, so no independent tailsafe coverage is being added for the
migration directory under that decision. Initial duplicate keys in
`infra-ops/prd` remain pending separate deletion review.

The OpenShift 4.22.15 etcd backup completed without stopping Knowhere.
`altus-etcd-20261027T060937Z.tgz.gpg` contains one 186,597,408-byte snapshot and
static resources, passed decrypt-and-tar-list verification, and has matching
local/NAS SHA-256 `e44781dd83b4b3652edb6b51e23992f93eb68de8059b33a25eb57bc0b7a15722`.
Only GPG ciphertext was written off Altus; the exact temporary plaintext
directory on Altus was removed after remote verification. No cluster upgrade is
permitted between this backup and the migration.

All 52 repositories passed sequential `restic snapshots --latest=1` reachability
checks with current snapshots. The evidence archive
`altus-gate-a-repository-reachability-20261027.tar.gz` is NAS-verified with
SHA-256 `b4b92a27a5613b2782b3084c50c69d724e3fac9a8cb3cd9f4767b02b94e1b251`.
Tailsafe is running and mounts the local restic repository read-only. These are
reachability/protection checks, not B00 integrity or restore passes.

All 11 database schema/table/count baselines completed sequentially and are in
NAS-verified `altus-gate-a-db-baselines-20261027.tar.gz` (SHA-256
`90a49986f826a34150c605d46499fdb6ceb754637f4e16c005269566e17d5d3d`).
Eleven direct filesystem manifests also completed with numeric ownership,
allocation, inode, and sparse-file metadata in
`altus-gate-a-filesystem-direct-20261027.tar.gz` (SHA-256
`015f46ffdb7c6a8442b22ab98378354099b75d73755dc78d12dc1308d44419f8`).
GNU `find` ownership formats in this runbook use `%U:%G`; `stat` continues to
use `%u:%g` for numeric IDs.

Knowhere's live root, projects, and PVC-wrapper manifests also completed without
guest interruption. Because the root path manifest produced 126 generic
API-key-pattern findings (duplicated in raw/sorted copies), the complete evidence
was uploaded only as encrypted
`altus-gate-a-knowhere-filesystem-20261027.tar.gz.gpg`, with verified decryption
and NAS SHA-256 `4874e06e9be46c4ca636a755144d25c77640ab8c32d7aa2e4cb35303203a086d`.
No plaintext Knowhere manifest archive was uploaded.

GitOps freeze ownership and ordering are reviewed: this migration session owns
the parent-first/child-second freeze and must record every affected Application.
Auto-sync remains enabled during read-only baselines and will be frozen only for
controlled volume-reference changes, then restored after final reconciliation.

All nine detached/snapshot/helper baselines completed with scratch cleanup in
NAS-verified `altus-gate-a-filesystem-helpers-20261027.tar.gz` (SHA-256
`6081e5c9b15f4788d342004802ab1ecfcd5643fcd0063ac3ebda2af957d307b5`).
V06 required its restored `s0:c45,c1004` label; O03–O05 required read-only
`spc_t` for unlabeled filesystems. The three coordinated Nextcloud NFS, Immich
library, and Paperless export baselines completed in
`altus-gate-a-filesystem-additional-20261027.tar.gz` (SHA-256
`85145cdba5a71bd56e72e387f764d6976aa3092a1686f2c001f73c7f2a79e350`);
Nextcloud's direct scan used the application UID/GID 33 without changing access.

The baseline set is complete, but Gate A remains open on final activity: the
pre-existing `influxdb3/volsync-src-data-backup` mover has been stuck in
`CreateContainerError` with `RST_STREAM ... CANCEL` since its scheduled run.
This is outside the migration PVC set and was not restarted or modified by the
migration work. No B00–B24 batch or production migration has run.

### Gate A: preparation

- [x] Live inventory reconciled by name to all 104 production route IDs; all
      source PVCs Bound. M01/M02 are live routes; V03/O01 are DR-only coverage.
- [x] Current PV/PVC/DataVolume/VM and workload metadata exported without Secrets.
- [x] Baseline numeric ownership, file counts, sizes, and application-specific
      database table/row counts captured, including coordinated NFS/library/export data.
- [x] Approved NFS directory is writable by the all_squash identity 1024:100;
      current measurements remain within the approved 370.807GiB budget or the
      reviewed 604.357GiB worst-case full-root budget if optional O01 is approved.
- [x] Every required restic repository on `/volume1/configs` passed sequential
      reachability/latest-snapshot checks; the existing tailsafe container is
      running and mounts the local repository read-only. B00 integrity remains separate.
- [x] The migration directory's missing tailsafe coverage is covered by explicit
      operator NAS-only risk acceptance dated 2026-10-27; it is not independently
      protected.
- [x] Required ExternalSecrets are `SecretSynced`; mounted passphrase files are
      0400, nonempty, readable only by the helper, and never logged. The root LUKS
      Secret is required for the running-production root safety copy and for
      separately approved optional O01 DR work.
- [x] The pinned virt-launcher digest provides qemu-img 10.1, GNU tar with
      xattr/ACL/SELinux/sparse support, GnuPG AES256, and `sha256sum`; the separate
      pinned libguestfs helper passed synthetic read-only inspection and write
      denial using the environment and writable mounts recorded above.
- [x] The `knowhere-lan` OVN localnet mapping is configured and validated on
      xandar before any first-leg plan is created. A disposable VMI on xandar has
      proven the expected VLAN, addressing, routes and peer connectivity without
      using Knowhere's production network identity.
- [x] Knowhere reports `StorageLiveMigratable=True` and `BlockMigration`;
      Altus/xandar CPU, scheduling, storage and network prerequisites pass.
      Ordinary `LiveMigratable=False` from local RWO PVCs is expected and is not
      a storage-migration failure. Both plans name M01 and M02 together, use
      unique destination PVCs, and explicitly set `retentionPolicy: keepSource`.
- [x] Knowhere GitOps freeze/unfreeze owners and exact VM/DataVolume/PVC/VolSync
      reference changes are reviewed. Interim VolSync uses `synology-snapshot`
      and a cache off `lvms-vg1`; final uses `lvms-knowhere` for snapshot/cache.
      Freeze activates only for controlled volume-reference changes, not read-only baselines.
- [x] Encrypted off-node etcd backup completed under the separate storage plan;
      OpenShift 4.22.15 artifact and NAS checksum verified on 2026-10-27.
- [ ] No unresolved cluster degradation, CI activity, deployments, or active
      migration streams.

### Gate B: restore tests and staged live rehearsal

- [ ] Representative restic filesystem restores compared for content and ownership.
- [ ] All 11 database dump types imported into isolated instances/namespaces and
      checked with real table/row counts and extension/application checks.
- [ ] **Running-production root safety passed:** a crash-consistent root
      VolumeSnapshot, or a documented guest-filesystem-frozen snapshot, was
      restored only to scratch, copied to encrypted off-node qcow2, compared in
      full, and passed read-only inspection plus the reviewed isolated recovery
      boot. Production Knowhere never stopped and its source was never attached
      to a helper or second VM.
- [ ] **B18a passed before rebuild:** a disposable VM with one 300Gi Block root and
      one 136579960013-byte Filesystem projects disk completed one two-disk live
      plan from `lvms-vg1` to `synology-iscsi-storage` while moving Altus → xandar.
      `retentionPolicy: keepSource` retained both sources; interim VolSync active-PVC,
      `synology-snapshot`, non-vg1 cache and fresh successful sync were proven.
- [ ] **B18b passed after rebuild and before production leg 2:** the same
      still-running disposable VM moved both active iSCSI disks together to
      `lvms-knowhere` while moving xandar → Altus, retained interim sources, and
      proved final active-PVC/snapshot/cache changes plus a fresh successful sync.
      B18b cannot be required before `lvms-knowhere` exists.
- [ ] Across B18a/B18b, continuous console/SSH, application, network and
      root/projects I/O probes remained within the approved brief-live-migration-
      pause budget, with no shutdown, reboot, unexplained gap, duplicate or corrupt
      write. Guest boot identity, mounts and projects data stayed unchanged. The
      rehearsal used no production VM/PVC/MAC and caused no production downtime.
- [ ] V03 projects restore evidence is isolated DR-test evidence only. It is not a
      production restore route and cannot substitute for B18. Optional O01 qcow2
      DR restore/compare evidence, if separately approved, likewise cannot
      substitute for B18 or M01. No halted-production Knowhere test is permitted.
- [x] KNX restore evidence revalidated on 2026-10-29 against fresh V04
      `8e7bb8ab` and V05 `3a65d8ef` backups taken while production was
      Stopped/Halted with no VMI or source attachments. Full Restic read-data,
      exact Direct restores, firmware/NVRAM/vTPM state, exact mixed metadata,
      SELinux accessibility and UID/GID 107 read-only checks passed. Non-boot
      exact restore validation satisfied the revised contract; an earlier
      isolated Windows boot also passed. Interactive ETS authentication and KNX
      live migration are excluded; production KNX remained stopped.
- [ ] Encrypted offline filesystem archives decrypted/extracted into scratch
      storage; file counts, checksums, numeric owners, sparse allocation, and
      destination SELinux accessibility checked.
- [ ] Every required repository passed `restic check`; full or sampled read-data
      scope and result recorded.
- [ ] Gate B evidence includes dates, artifact IDs, migration/plan names, source
      and target PV names, node transitions, continuous-availability logs, command
      exit statuses, and operator sign-off. The synthetic helper tests are helper
      evidence only; B18 and production root safety evidence are still pending.

## Final backup sequence (maintenance window)

### 1. Quiesce writers and prove detach

1. Pause CI and deployments. Disable affected ArgoCD auto-sync parent-first.
2. Scale non-Knowhere application Deployments/StatefulSets to zero in dependency
   order: application workers first, databases last. Keep a database available
   only for its final logical dump. Knowhere has no scale-down step and stays up.
3. Gitea: stop runners and Gitea writers; dump D01 and back up V02 before
   stopping `gitea-db`.
4. Nextcloud: enable maintenance mode, dump D06, complete V07 and the separate
   NFS user-data sync, then stop MariaDB. Keep maintenance mode enabled until
   restore validation completes; do not let a scheduled CronJob disable it early.
5. Paperless: stop web/workers/scheduler/exporter, dump D07, then sync V08/V09.
6. Monitoring: pause the managing operators before scaling their stateful
   workloads, so they do not recreate writers. Record original replica counts
   and management state. Stop cluster monitoring before its Prometheus and
   Alertmanager; stop user-workload monitoring control before UWM Prometheus and
   Thanos Ruler. Do not change retention or history.
7. Registry: record `configs.imageregistry.operator.openshift.io/cluster`
   management state and replicas, set the operator unmanaged under the approved
   cutover procedure, scale `image-registry` to zero, and verify no registry pod
   remains. Restore `Managed` only after data validation.
8. Paperless-mcp: scale its app-template controller to zero and verify both MCP
   and proxy containers are gone.
9. KNX only: halt the VM and wait for its VMI to disappear. Never rely on a guest
   shutdown request alone. Knowhere follows the live M01/M02 procedure instead;
   do not stop it or attach its active production disks to offline helpers.

Read-only proof before each offline helper starts:

```bash
oc-sa altus get pods -A -o wide | grep -E 'knowhere|windows-11|prometheus|alertmanager|thanos-ruler|image-registry|paperless-mcp'
oc-sa altus get volumeattachments.storage.k8s.io
oc-sa altus get vm,vmi -n knowhere
oc-sa altus get vm,vmi -n knx-ets
# The relevant non-Knowhere writer and attachment must be absent. Inspect any
# match; do not treat an empty grep exit status as a failed preflight. Knowhere's
# running VMI is expected and is never an offline-helper source in this process.
```

### 2. Take final logical database dumps

Use the existing CronJob definitions so Secret references and server-side
password handling remain unchanged. Trigger one Job per D row and wait for its
new completion; do not accept an older successful Job. For Nextcloud use
`database-mariadb-backup`; the others use `database-psql-backup`.

```bash
NS=gitea                         # one D namespace at a time
JOB="db-final-$(date +%s)"
oc-sa altus -n "$NS" create job "$JOB" --from=cronjob/database-psql-backup
oc-sa altus -n "$NS" wait --for=condition=Complete "job/$JOB" --timeout=30m
oc-sa altus -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}{"\n"}'
```

The Nextcloud command differs only in
`--from=cronjob/database-mariadb-backup`. Suspend or otherwise prevent unrelated
scheduled database jobs during the coordinated window so they cannot change
maintenance mode or replace `backup.sql` between verification and VolSync sync.

For every D row, mount its dump-staging PVC in a one-shot helper after the dump
writer has finished and record:

```bash
test -s /backup/backup.sql
stat -c '%n %s %y' /backup/backup.sql
sha256sum /backup/backup.sql
# Do not cat the dump. SQL can contain role credentials and application data.
```

Then trigger every listed ReplicationSource for that dump. Immich, Nextcloud,
Paperless, Tymeslot, and Vaultwarden have both local and B2 dump repositories;
verify both. A successful Job with a zero-byte or stale `backup.sql` is a failed
backup.

### 3. Trigger and verify VolSync final backups

For each V and D repository, patch a unique manual trigger. Trigger all local
and B2 legs listed in the inventory. Do not run all streams concurrently.

```bash
NS=paperless
RS=media-backup-b2
TOKEN="lvms-final-$(date +%s)"
BEFORE="$(oc-sa altus -n "$NS" get replicationsource "$RS" -o jsonpath='{.status.lastSyncTime}')"
oc-sa altus -n "$NS" patch replicationsource "$RS" --type merge \
  -p "{\"spec\":{\"trigger\":{\"manual\":\"$TOKEN\"}}}"
# Poll this specific RS until lastSyncTime is newer than BEFORE and
# status.latestMoverStatus.result is Successful. Never infer success from the pod alone.
oc-sa altus -n "$NS" get replicationsource "$RS" \
  -o jsonpath='{.spec.trigger.manual}{"\t"}{.status.lastSyncTime}{"\t"}{.status.latestMoverStatus.result}{"\n"}'
```

After verification, remove every manual trigger without removing the schedule:

```bash
oc-sa altus -n "$NS" patch replicationsource "$RS" --type json \
  -p '[{"op":"remove","path":"/spec/trigger/manual"}]'
oc-sa altus -n "$NS" get replicationsource "$RS" -o jsonpath='{.spec.trigger.schedule}{"\n"}'
```

Verify the latest restic snapshot and repository from a purpose-built Job/Pod
using `envFrom.secretRef` on the existing repository Secret. Use an emptyDir for
`RESTIC_CACHE_DIR`; the cache is disposable. For B2, the existing ExternalSecret
also injects the Backblaze credential variables. Do not place repository URLs or
passwords in the Pod spec.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: restic-check-once
  namespace: paperless
spec:
  restartPolicy: Never
  containers:
    - name: restic
      image: restic/restic:0.18.0
      envFrom:
        - secretRef:
            name: restic-config-media-b2
      env:
        - name: RESTIC_CACHE_DIR
          value: /cache
      command: ["/bin/sh", "-ec"]
      args:
        - |
          restic snapshots --latest=1
          restic check --read-data-subset=5%
      volumeMounts:
        - name: cache
          mountPath: /cache
  volumes:
    - name: cache
      emptyDir: {}
```

Use the installed/tested restic image version rather than upgrading tools during
cutover. Record the latest snapshot ID/timestamp and check scope. For high-value
local-only repositories, use a full `restic check --read-data` before Gate C if
time and NAS load permit.

### 4. Offline filesystem copy pattern

Use one helper per O02–O07 volume. The source is mounted read-only and the NFS
destination is the approved dedicated directory, never a PVC backed by `vg1`.
Tar writes to stdout and GnuPG writes only ciphertext to NFS; no plaintext tar or
temporary plaintext file is created. Use a dedicated namespace/service account
and only the SCC required for numeric-owner preservation; remove temporary RBAC
during approved cleanup.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: offline-filesystem-backup
  namespace: openshift-monitoring
spec:
  restartPolicy: Never
  serviceAccountName: lvms-migration-helper
  securityContext:
    runAsUser: 0
  containers:
    - name: copy
      image: registry.redhat.io/container-native-virtualization/virt-launcher-rhel9@sha256:2131d3d24616971b92d7e986635673b6dc731da3d43cae46540882993d36596f
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: [ALL]
          add: [CHOWN, DAC_OVERRIDE, FOWNER]
      command: ["/bin/bash", "-ec"]
      args:
        - |
          set -o pipefail
          umask 077
          export GNUPGHOME=/tmp/.gnupg
          mkdir -p "$GNUPGHOME"
          chmod 700 "$GNUPGHOME"
          test -n "${ARTIFACT:?}"
          test ! -e "/backup/$ARTIFACT.tar.gpg"
          tar --one-file-system --numeric-owner --sparse --xattrs --acls --selinux \
            -cpf - -C /src . | \
            gpg --batch --no-tty --pinentry-mode loopback \
              --passphrase-file /run/secrets/migration/filesystem-gpg.passphrase \
              --symmetric --cipher-algo AES256 \
              --output "/backup/$ARTIFACT.tar.gpg"
          test -s "/backup/$ARTIFACT.tar.gpg"
          gpg --batch --no-tty --pinentry-mode loopback \
            --passphrase-file /run/secrets/migration/filesystem-gpg.passphrase \
            --decrypt "/backup/$ARTIFACT.tar.gpg" | tar -tf - >/dev/null
          sha256sum "/backup/$ARTIFACT.tar.gpg" > "/backup/$ARTIFACT.tar.gpg.sha256"
          stat -c '%n %s bytes %b blocks' "/backup/$ARTIFACT.tar.gpg"
          sync
      env:
        - name: ARTIFACT
          value: O04-prometheus-k8s-final-YYYYMMDDTHHMMSSZ
      volumeMounts:
        - name: source
          mountPath: /src
          readOnly: true
        - name: backup
          mountPath: /backup
        - name: encryption
          mountPath: /run/secrets/migration
          readOnly: true
  volumes:
    - name: source
      persistentVolumeClaim:
        claimName: prometheus-k8s-db-prometheus-k8s-0
        readOnly: true
    - name: backup
      nfs:
        server: 10.10.10.40
        path: /volume1/pmb-backup/altus-lvms-migration
    - name: encryption
      secret:
        secretName: altus-lvms-filesystem-encryption
        defaultMode: 0400
        items:
          - key: passphrase
            path: filesystem-gpg.passphrase
```

Create a fresh artifact path for each run; never overwrite the last good copy.
The manifest is the O04 example; substitute the exact O02/O03/O05/O06/O07 claim,
namespace, and artifact ID. Both pipelines use `pipefail`, so a tar, GnuPG,
decryption, or listing failure fails the helper. Do not use `kubectl cp`, which
is unsuitable for sparse files, ownership, xattrs, and large streams.

Restore into a new, unmounted PVC with the same access mode and at least the
original requested size. Mount it at `/dst` in a new helper using the same pinned
image, approved NFS mount, 0400 filesystem-encryption Secret mount, namespaced
`anyuid` binding, and the three explicit capabilities shown above. Do not add
`SYS_ADMIN` or use `privileged`. Initialize a writable GnuPG home, then:

```bash
set -euo pipefail
umask 077
export GNUPGHOME=/tmp/.gnupg
mkdir -p "$GNUPGHOME"
chmod 700 "$GNUPGHOME"
test -z "$(find /dst -mindepth 1 -maxdepth 1 -print -quit)"
sha256sum -c /backup/O04-prometheus-k8s-final-YYYYMMDDTHHMMSSZ.tar.gpg.sha256
gpg --batch --no-tty --pinentry-mode loopback \
  --passphrase-file /run/secrets/migration/filesystem-gpg.passphrase \
  --decrypt /backup/O04-prometheus-k8s-final-YYYYMMDDTHHMMSSZ.tar.gpg | \
  tar --numeric-owner --same-owner --sparse --xattrs --acls --selinux \
    -xpf - -C /dst
find /dst -xdev -printf '%P\t%U:%G\t%m\t%y\t%s\n' | sort > /backup/owners-after.txt
diff -u /backup/owners-before.txt /backup/owners-after.txt
# Compare file manifests and representative/full checksums as scoped in evidence.
```

The decrypted bytes exist only in the pipeline and destination PVC, not on NFS.
Restore in the original namespace so the namespace's SELinux MCS allocation is
unchanged. Let the kubelet apply the destination volume context; do not blindly
copy a different namespace's MCS label in a scratch test. If `tar` cannot restore
SELinux xattrs, stop and resolve context with the installed policy/tooling rather
than starting a workload with an unreadable volume. Verify both `stat` numeric
ownership and `ls -Z` accessibility from the eventual workload security context.

## Knowhere 300Gi Block root: optional O01 DR-only qcow2-LUKS reference

This section is retained only for a future, separately approved disaster-recovery
backup. It is not an executable step in the live migration, not a Gate B/C
requirement, and never a production migration mechanism. Do not halt production
Knowhere to create this artifact or to test this process. M01 and the B18 live
rehearsal are the production root-disk route and its required proof.

### Optional, separately approved DR backup

O01 is a raw 300Gi block PVC. The DR backup is a self-contained qcow2-LUKS image
with no backing file. Zero/unallocated blocks remain sparse on the NFS target;
do not `dd` a full 300Gi stream. Compression is optional and should be decided
by the tested throughput/CPU baseline; sparseness, LUKS encryption, and integrity
are mandatory. Only ciphertext is written to NFS.

The helper needs a `volumeDevice`, not a `volumeMount`, and runs only after the
Knowhere VMI and source attachment are gone. `QCOW2_OPTS` describes the already
created encrypted image for check/info/compare. With `--image-opts`, neither
`-f` nor `-F` is accepted; the raw comparison input is therefore a second
explicit `driver=raw,file.driver=host_device,file.filename=...` option string.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: knowhere-root-offline-backup
  namespace: knowhere
spec:
  restartPolicy: Never
  serviceAccountName: lvms-migration-helper
  securityContext:
    runAsUser: 0
  containers:
    - name: qemu
      image: registry.redhat.io/container-native-virtualization/virt-launcher-rhel9@sha256:2131d3d24616971b92d7e986635673b6dc731da3d43cae46540882993d36596f
      command: ["/bin/bash", "-ec"]
      args:
        - |
          umask 077
          test -b /dev/source
          test -n "${ARTIFACT:?}"
          test ! -e "/backup/$ARTIFACT.qcow2"
          qemu-img convert --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            -p -f raw -O qcow2 \
            -o cluster_size=2M,preallocation=off,encrypt.format=luks,encrypt.key-secret=rootkey \
            /dev/source "/backup/$ARTIFACT.qcow2"
          test -s "/backup/$ARTIFACT.qcow2"
          QCOW2_OPTS="driver=qcow2,file.driver=file,file.filename=/backup/$ARTIFACT.qcow2,encrypt.format=luks,encrypt.key-secret=rootkey"
          qemu-img check --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            --image-opts "$QCOW2_OPTS"
          qemu-img info --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            --backing-chain --image-opts "$QCOW2_OPTS"
          qemu-img compare --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            -p --image-opts "$QCOW2_OPTS" \
            "driver=raw,file.driver=host_device,file.filename=/dev/source"
          sha256sum "/backup/$ARTIFACT.qcow2" > "/backup/$ARTIFACT.qcow2.sha256"
          stat -c '%n apparent=%s allocated-blocks=%b' "/backup/$ARTIFACT.qcow2"
          du -h "/backup/$ARTIFACT.qcow2"
          sync
      env:
        - name: ARTIFACT
          value: O01-knowhere-root-final-YYYYMMDDTHHMMSSZ
      volumeDevices:
        - name: source
          devicePath: /dev/source
      volumeMounts:
        - name: backup
          mountPath: /backup
        - name: encryption
          mountPath: /run/secrets/migration
          readOnly: true
  volumes:
    - name: source
      persistentVolumeClaim:
        claimName: knowhere-rootdisk
        readOnly: true
    - name: backup
      nfs:
        server: 10.10.10.40
        path: /volume1/pmb-backup/altus-lvms-migration
    - name: encryption
      secret:
        secretName: altus-lvms-knowhere-root-encryption
        defaultMode: 0400
        items:
          - key: passphrase
            path: root-luks.passphrase
```

`qemu-img compare` is intentionally a complete read-back verification even
though it adds time. A nonzero exit, corrupt qcow2 metadata, missing/incorrect
LUKS encryption, backing-file reference, missing checksum, or unexpected 300Gi
virtual size fails the optional DR backup. There is no filesystem ownership to
`chown` inside this raw block backup.

### Optional isolated DR restore test

If O01 is separately approved, test only an existing DR artifact in a scratch
namespace; never halt production to create a test input. Use a
**non-production target StorageClass approved for the test**, with at least
300Gi virtual capacity and enough real allocation. Do not use a running VM, a
production DataVolume name, a production MAC, or the old block PV. Materialize
`altus-lvms-knowhere-root-encryption` in the scratch namespace with an
ExternalSecret referencing the same root Doppler key; do not copy Secret data.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: knowhere-root-restore-test
  namespace: knowhere-restore-test
spec:
  accessModes: [ReadWriteOnce]
  volumeMode: Block
  storageClassName: <APPROVED_NONPRODUCTION_TEST_CLASS>
  resources:
    requests:
      storage: 300Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: knowhere-root-restore-test
  namespace: knowhere-restore-test
spec:
  restartPolicy: Never
  serviceAccountName: lvms-migration-helper
  securityContext:
    runAsUser: 0
  containers:
    - name: qemu
      image: registry.redhat.io/container-native-virtualization/virt-launcher-rhel9@sha256:2131d3d24616971b92d7e986635673b6dc731da3d43cae46540882993d36596f
      command: ["/bin/bash", "-ec"]
      args:
        - |
          umask 077
          test -b /dev/target
          ARTIFACT=O01-knowhere-root-final-YYYYMMDDTHHMMSSZ
          sha256sum -c "/backup/$ARTIFACT.qcow2.sha256"
          QCOW2_OPTS="driver=qcow2,file.driver=file,file.filename=/backup/$ARTIFACT.qcow2,encrypt.format=luks,encrypt.key-secret=rootkey"
          qemu-img check --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            --image-opts "$QCOW2_OPTS"
          qemu-img convert --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            -p --image-opts -O raw -S 4k "$QCOW2_OPTS" /dev/target
          qemu-img compare --object secret,id=rootkey,file=/run/secrets/migration/root-luks.passphrase \
            -p --image-opts "$QCOW2_OPTS" \
            "driver=raw,file.driver=host_device,file.filename=/dev/target"
          # In the separate pinned libguestfs helper, inspect the restored raw
          # target read-only: partition table, boot/root filesystems, and a known
          # non-secret file. Never mount the restored filesystem read-write.
      volumeDevices:
        - name: target
          devicePath: /dev/target
      volumeMounts:
        - name: backup
          mountPath: /backup
        - name: encryption
          mountPath: /run/secrets/migration
          readOnly: true
  volumes:
    - name: target
      persistentVolumeClaim:
        claimName: knowhere-root-restore-test
    - name: backup
      nfs:
        server: 10.10.10.40
        path: /volume1/pmb-backup/altus-lvms-migration
    - name: encryption
      secret:
        secretName: altus-lvms-knowhere-root-encryption
        defaultMode: 0400
        items:
          - key: passphrase
            path: root-luks.passphrase
```

The verified virt-launcher image does not provide `guestfish` or
`virt-filesystems`; use the separate pinned libguestfs helper with `guestfish --ro`
for the optional DR inspection step. Its synthetic runtime test passed with the
direct-backend environment and writable mounts recorded above. Do not make scratch
sparse-file guarantees a substitute for content comparison. Record the restored
PV, virtual size, comparison exit status, inspected partitions/filesystems, and
known-file result if this DR test is approved. Passing it does not close B18 or
Gate B: B18 requires the disposable two-leg live rehearsal with continuous
availability.

## Knowhere production execution: two live storage-migration legs

Gate A, the other required Gate B tests, and B18a must pass before production leg
1; B18b must pass after rebuild and before production leg 2. There is no
production halt, blank-root DataVolume restore, V03 production restore, or
same-node/direct migration step.
The same running Knowhere VM and guest remain available throughout. Record the
VMI UID/start time, guest boot ID, active DataVolume/PVC names, volume names,
node, projects mount, network and a real agent/session probe before starting.
Run continuous timestamped console/SSH, application, network, root/projects I/O
and availability probes until final validation ends, using B18's approved brief
live-migration-pause budget. Any guest stop/restart, unexplained availability
gap, or probe failure beyond that budget stops the process and blocks rebuild.

Freeze Knowhere GitOps before each leg and keep it frozen while the migration
controller changes VM volume references. Review and apply the corresponding VM,
DataVolume/PVC and VolSync reference changes as one deliberate reconciliation;
do not let ArgoCD restore an old reference or prune a retained source. GitOps
confirms the VM volume names `rootdisk` and `projectsdisk`; revalidate them for
drift before applying either plan. Preserve the `projectsdisk` serial and all
network/cloud-init identity.

### Leg 1: both disks to iSCSI; running VM Altus → xandar

1. Complete Gate A's xandar `knowhere-lan` OVN localnet configuration and
   disposable-network validation. Reconfirm storage-live-migratability, capacity
   and continuous probes. Require the fresh running-production root safety
   snapshot/scratch recovery evidence and a fresh projects sync before leg 1.
   A missing/invalid xandar mapping or stale safety artifact blocks leg 1.
2. Create one plan containing **both** M01 and M02, with unique interim PVC names
   and `synology-iscsi-storage`. `retentionPolicy: keepSource` is mandatory even
   if it is the controller default:

   ```yaml
   apiVersion: migrations.kubevirt.io/v1alpha1
   kind: VirtualMachineStorageMigrationPlan
   metadata:
     name: knowhere-live-leg1
     namespace: knowhere
   spec:
     retentionPolicy: keepSource
     virtualMachines:
       - name: knowhere
         targetMigrationPVCs:
           - volumeName: rootdisk
             destinationPVC:
               name: knowhere-rootdisk-iscsi
               storageClassName: synology-iscsi-storage
               volumeMode: Block
               accessModes: [ReadWriteOnce]
           - volumeName: projectsdisk
             destinationPVC:
               name: knowhere-projects-iscsi
               storageClassName: synology-iscsi-storage
               volumeMode: Filesystem
               accessModes: [ReadWriteOnce]
   ```

3. Start that plan with a `VirtualMachineStorageMigration`; the plan alone does
   not execute:

   ```yaml
   apiVersion: migrations.kubevirt.io/v1alpha1
   kind: VirtualMachineStorageMigration
   metadata:
     name: knowhere-live-leg1
     namespace: knowhere
   spec:
     virtualMachineStorageMigrationPlanRef:
       name: knowhere-live-leg1
   ```

4. Watch the storage migration, underlying VMIM, VM/VMI, DataVolumes and PVCs.
   Require `Completed`, both active disk references on the interim iSCSI PVCs,
   the running VMI on xandar, and unchanged guest boot identity. Verify
   `knowhere-lan`, projects mount and application/agent probes. Do not proceed if
   only one disk migrated or the VM stopped. Keep both original `lvms-vg1`
   sources; there is no automatic deletion.
5. While GitOps remains frozen, update its active projects volume reference to
   `knowhere-projects-iscsi`, the projects ReplicationSource `spec.sourcePVC` to
   that same active PVC, `spec.restic.volumeSnapshotClassName` to
   `synology-snapshot`, and `spec.restic.cacheStorageClassName`/R10 to an approved
   class off `lvms-vg1`. Keep `copyMethod: Snapshot` so the mover does not mount
   and relabel the live projects filesystem. Reconcile deliberately, trigger a new
   manual projects sync, and require a fresh `lastSyncTime` plus
   `latestMoverStatus.result=Successful`. Record snapshot/repository evidence and
   remove only the manual trigger. Do not continue to rebuild until this backup
   and all other Gate C rows pass.

Any failed or incomplete leg stops the rebuild. Deleting the migration object to
cancel is permitted only before completion and its outcome must be verified; after
`Completed`, cancellation/deletion is not rollback and must never be presented as
a way back to `lvms-vg1`.

### Rebuild vg1 and create lvms-knowhere

Only after production leg 1, its fresh successful projects sync, disposable B18a,
and every required Gate C backup/restore row pass may the separate storage plan
rebuild `vg1` and create `lvms-knowhere`. Keep production Knowhere and the B18
disposable guest running on xandar/iSCSI. Validate the new class and
DataSource/image imports, then complete B18b before production leg 2. Any failed
leg-1/B18a check, missing backup, or unsuccessful sync means **do not rebuild**.
`keepSource` retains the original vg1 M01/M02 sources until this point. The
rebuild's reviewed teardown must explicitly name and authorize their deletion;
the migration controller must never delete them automatically. Preserve all
off-pool backups and rollback evidence through the observation window.

### Leg 2: both active disks to lvms-knowhere; running VM xandar → Altus

1. Require B18b signed off. Freeze Knowhere GitOps again. Confirm the running VMI
   is on xandar, both active disks are the interim iSCSI PVCs, continuous probes
   are healthy, Altus localnet mapping and LVMS capacity are ready, and no old vg1
   volume is referenced. Refresh the independent root safety snapshot/copy and
   projects sync from the active iSCSI volumes before starting leg 2.
2. Create `knowhere-live-leg2` as one `VirtualMachineStorageMigrationPlan` with
   `retentionPolicy: keepSource` and both `rootdisk` and `projectsdisk` targets.
   Use unique final PVC names `knowhere-rootdisk-lvms` (Block) and
   `knowhere-projects-lvms` (Filesystem), both RWO with
   `storageClassName: lvms-knowhere`. Start its matching
   `VirtualMachineStorageMigration`; never run separate root/projects migrations.
3. Require `Completed`, both active disk references on the final PVCs, the same
   running guest back on Altus, unchanged boot identity, healthy `knowhere-lan`,
   projects mount, root/projects I/O, networking, Tailscale/herdr, and a real agent
   session. Run `mise run knowhere:verify` without stopping the guest.
4. Update GitOps active projects/VM references to `knowhere-projects-lvms`, the
   ReplicationSource `spec.sourcePVC` to that same final active PVC,
   `spec.restic.volumeSnapshotClassName` and
   `spec.restic.cacheStorageClassName`/R10 to `lvms-knowhere`. Keep Snapshot copy
   mode, then trigger and verify another fresh successful projects sync as above.
   Unfreeze only after VM, storage, VolSync and guest evidence agrees.

Keep the interim iSCSI M01/M02 source PVCs/PVs/DataVolumes for the defined
observation window; do not delete them when leg 2 completes. Original vg1 source
deletion belongs only to the separately reviewed rebuild teardown above. Later
interim-iSCSI teardown requires another review after healthy observation and
verified final backups. No source cleanup is automatic or implied by `Completed`.
A completed leg cannot be rolled back by cancellation; recovery requires a
separately reviewed forward plan while preserving Knowhere availability.

## VolSync restore pattern

Destination PVCs must already exist, be Bound, have the intended class, mode,
size, and access mode, and have no writer. Use the repository Secret materialized
by the app's existing ExternalSecret. For a scratch namespace, recreate the
ExternalSecret against the same Doppler keys; do not copy a live Secret.

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: external-restic-restore
  namespace: <DESTINATION_NAMESPACE>
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler-cluster
  target:
    name: restic-config-data
  data:
    - secretKey: RESTIC_REPOSITORY
      remoteRef:
        key: <EXISTING_APP_REST_REPO_KEY>
    - secretKey: RESTIC_PASSWORD
      remoteRef:
        key: RESTIC_PASSWORD
```

For B2, follow `templates/volsync/backblaze/external-restic-config-b2.yaml` and
also include `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from the existing
Backblaze Doppler keys. For local restore, follow
`templates/volsync/rest/external-restic-config-rest.yaml`. Verify the
ExternalSecret's `SecretSynced` condition, not Secret contents.

This is the `templates/replicationdestination.yaml` pattern:

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: paperless-media-restore-once
  namespace: paperless
spec:
  trigger:
    manual: lvms-restore-YYYYMMDDTHHMMSSZ
  restic:
    repository: restic-config-media-b2
    destinationPVC: paperless-ng-media
    copyMethod: Direct
```

For V13 only, add the existing mover context. Omit `fsGroup` and
`fsGroupChangePolicy` so the mover does not rewrite restored metadata:

```yaml
    moverSecurityContext:
      runAsUser: 10000
      runAsGroup: 10000
      runAsNonRoot: true
```

Wait for `.status.latestMoverStatus.result: Successful` on the specific restore,
and confirm the completion time is the current operation. A `Synchronizing=false`
condition alone does not prove success. Keep the ReplicationDestination until
its evidence is recorded; remove temporary restore objects only in the approved
cleanup phase. Do not remove or prune source snapshots.

KNX has a proven namespace-isolated procedure in
`docs/runbooks/knx-ets-volsync-restore-test.md`. Restore V04 and V05 together;
verify `disk.img`, NVRAM, and
`swtpm/b73ae2a8-c599-45f0-9830-33e50088fae3/tpm2/tpm2-00.permall`, with 107:107
ownership. Preserve the production firmware UUID and persistent-state label.
The non-production clone must use a different MAC and no USB dongle.

## Database restore pattern

### PostgreSQL / CNPG and Immich

1. Restore the dump-staging PVC from the selected repository with Direct.
2. Recreate the D-row data PVC empty. Start only the new database and wait for
   readiness. All application Deployments/workers remain at zero.
3. Resolve the current CNPG primary from `.status.currentPrimary`; do not assume
   `-db-1` after failover. For Immich use `deploy/immich-app-database`, container
   `database`.
4. Confirm the database is the newly initialized target, not the source. Record
   target pod UID and new PV name before any `DROP DATABASE`.
5. CNPG auto-creates the application database. Drop and recreate that empty
   application database using the exact D-table database/owner, then replay the
   full `pg_dumpall` dump. Existing global roles/databases can produce expected
   “already exists” diagnostics; do not treat a zero exit with unreviewed errors
   as success. Abort startup on any relation, extension, data, or permission
   error.
6. Immich's Deployment uses `POSTGRES_USER`/`POSTGRES_PASSWORD` from its existing
   Secret. Execute password use inside the container (`PGPASSWORD` server-side)
   and never echo it. Its custom image must retain the required vectorchord and
   pgvector extensions; verify extensions before application startup.
7. Compare exact table counts and real `count(*)` baselines for representative
   application tables, not `pg_stat` estimates. Then perform application-specific
   read/write checks after startup.

Guarded CNPG target reset example; substitute only exact D-table identifiers
after confirming the new target:

```bash
NS=gitea
CLUSTER=gitea-db
PRIMARY="$(oc-sa altus -n "$NS" get cluster.postgresql.cnpg.io "$CLUSTER" -o jsonpath='{.status.currentPrimary}')"
oc-sa altus -n "$NS" exec "$PRIMARY" --container postgres -- \
  psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE IF EXISTS gitea WITH (FORCE);' \
  -c 'CREATE DATABASE gitea OWNER gitea;'

# /backup/backup.sql is the verified restored dump in a separate one-shot helper.
oc-sa altus -n "$NS" exec db-restore-helper -- cat /backup/backup.sql | \
  oc-sa altus -n "$NS" exec -i "$PRIMARY" --container postgres -- \
    psql -U postgres -d postgres
```

Capture the import log in the protected evidence area and review every error.
Do not use `ON_ERROR_STOP=0` as a blanket waiver and do not print the SQL. If the
reviewed restore procedure requires a completely clean global-role target, build
that isolated target during Gate B rather than improvising during cutover.

### Nextcloud / MariaDB

Follow `docs/runbooks/nextcloud-restore.md`, including its server-side
`MARIADB_ROOT_PASSWORD_FILE` handling and empty-database race mitigation:

- Restore D06's dump and V07 in the same coordinated restore set, plus the
  separate `nextcloud-app-nextcloud-data` NFS source from its own restic
  repository. The NFS user-data volume is not one of the 104 pool PVCs but is
  required for a consistent application.
- Keep Nextcloud scaled to zero. Start MariaDB alone, drop/recreate the empty
  `nextcloud` database as documented, and stream the verified dump into MariaDB.
- Do not print the MariaDB password. The existing CronJob and restore runbook
  already use the correct Secret/file references.
- Before startup verify table/row counts, `php occ status` with `installed: true`,
  maintenance mode deliberately still on, and expected app/config/custom_apps
  state from V07. Disable maintenance mode only when ready for validation.
- Validate login, calendars, contacts, user files, and a real upload/download.
  If manually extracting into NFS user data, honor the existing uid/gid 33 and
  root-squash guidance in the Nextcloud runbook.

## Recreate and reimport

For R01–R65, let GitOps/VolSync/workload controllers create empty PVCs only after
both StorageClasses are Ready and test provisioning has passed. Do not restore
old DinD layers, runner caches, Redis state, model caches, or mover caches.
Confirm all 65 have an intentional empty successor and that new cache class
selection is correct.

For I01–I06, allow the six existing DataImportCrons to import once each into
`lvms-vg1`. Verify their DataSources point to Ready, current PVCs:

```bash
oc-sa altus -n openshift-virtualization-os-images get dataimportcron,datasource,datavolume,pvc
oc-sa altus -n openshift-virtualization-os-images get pvc -o json | jq -r '
  .items[] | [.metadata.name, .spec.storageClassName, .spec.volumeMode,
              .status.phase] | @tsv'
```

Do not launch duplicate imports to make names match I01–I06; those hash suffixes
are expected to change. Confirm all six DataSources are Ready before starting
dependent VM workloads.

## Restore validation and gates

### Gate C: final backups and live leg 1 complete; original pool still intact

Require one signed evidence row for every production M/V/D/O route:

- M01+M02: B18a passed; production leg-1 plan/migration names and `Completed`
  status; both disks moved together to iSCSI; same running guest moved Altus →
  xandar with unchanged boot identity and continuous availability; original vg1
  sources retained; GitOps active references reconciled; VolSync points to the
  interim active projects PVC with `synology-snapshot` and a cache off `lvms-vg1`;
  fresh successful projects sync and repository evidence recorded.
- V01/V02/V04–V15: fresh successful sync for every listed repository, latest
  snapshot identity/time, repository check, restore-test reference, ownership
  baseline. V03 also requires fresh DR-backup/sync coverage and its isolated DR
  restore-test reference, but is not a production restore row.
- D01–D11: new dump Job, nonzero `backup.sql`, byte size and SHA-256, fresh
  dump-PVC sync(s), repository check, isolated import and row-count evidence.
- O02–O07: stopped/detached proof, GnuPG decrypt-and-tar-list success with
  `pipefail`, ciphertext SHA-256, apparent and allocated size, scratch extraction
  comparison, and ownership/SELinux evidence; no plaintext `.tar` on NFS. O01 is
  optional separately approved DR coverage and is not required to unlock rebuild.
- R01–R65 and I01–I06: explicitly reviewed as recreation/reimport; no backup
  artifact is required.

Do not cross the storage point of no return with any missing or failed row.
Failure of leg 1 or its post-leg sync stops rebuild; completed-migration
cancellation is not rollback.

### Post-migration application checks and closeout

- Reconcile all 104 original names to their M/V/D/O/R/I successor. No active VM
  or workload may reference the previous VG generation. Any source PV retained
  for observation must be detached/unreferenced and does not count as active
  old-pool use; original vg1 sources are absent only after their reviewed rebuild
  teardown, never through automatic migration cleanup.
- Gitea: repository/file checks agree with D01; real CI job passes with DinD
  MTU/overlay2 checks.
- Nextcloud: installed flag, calendar/contacts/files, DB counts, and writable
  upload; no unintended maintenance mode left enabled.
- Paperless: data/media/DB agree; document search and a real document view work.
- Qdrant: collections, snapshots, crawl progress, and a real query/ingest check.
- Registry: catalog/content checks plus a real pull and push; restore operator
  management state.
- Monitoring: historical queries cover pre-migration ranges; Alertmanager
  silences/state and UWM/Thanos rules are present. No history trimming.
- Paperless-mcp: OAuth proxy state/clients remain valid and an authenticated MCP
  request succeeds without exposing tokens.
- Knowhere: B18b passed before production leg 2; leg 2 `Completed` with both
  active disks on `lvms-knowhere`, the same continuously available guest back on
  Altus, unchanged boot identity, healthy projects
  mount/network/Tailscale/herdr and real agent use. Final GitOps and
  VolSync reference `knowhere-projects-lvms`; snapshot/cache classes are
  `lvms-knowhere`; a fresh final projects sync succeeded. KNX must not request
  BitLocker recovery and must retain its vTPM state.
- Audit every ReplicationSource for fresh schedules, no leftover manual trigger,
  successful post-migration backups, and intended cache StorageClasses.
- Keep all backup repositories and migration artifacts unchanged for at least
  seven healthy days. Keep interim iSCSI M01/M02 sources for the observation
  window; do not prune rollback data or automatically delete source
  PVCs/PVs/DataVolumes. Original vg1 source deletion requires the explicit
  pre-rebuild teardown review; interim iSCSI teardown requires a separate review
  after observation and final backup validation. Record both reviewed decisions
  and the retained-source inventory in closeout.

## Unresolved safety questions / required Gate A–B decisions

1. **Migration-artifact independent protection:** the approved
   `/volume1/pmb-backup/altus-lvms-migration/` directory is not currently
   tailsafe-covered. Add and verify equivalent independent protection, or record
   explicit operator acceptance of the NAS-resident-only risk for the defined
   migration/retention window. Existing `/volume1/configs` tailsafe coverage does
   not protect the O02–O07 artifacts or optional O01 DR artifacts.
2. **Helper RBAC:** synthetic qcow2/GPG and libguestfs tests passed with
   namespaced `anyuid`, no privilege escalation, and only the documented minimum
   capabilities/environment. Validate the same permissions in each required
   production namespace; do not broaden them to `privileged` or `SYS_ADMIN`.
3. **Monitoring quiescence:** validate the exact operator pause/scale sequence
   against the installed Cluster Monitoring Operator and UWM versions, including
   who recreates pods and how original management/replica state is restored.
4. **Registry quiescence:** approve the `Managed` → `Unmanaged` → scale-down
   sequence and confirm no image-pruner/writer remains during the copy.
5. **Database globals:** prove the full `pg_dumpall` role/database restore behavior
   for each CNPG version and Immich's custom PostgreSQL image. Record expected
   duplicate-object diagnostics and fail criteria before the final import.
6. **Knowhere safety and B18:** run the running-production root snapshot through
   encrypted off-node copy and scratch recovery without stopping the source.
   Pass the disposable Block-root/Filesystem-projects two-leg rehearsal with
   continuous availability, `retentionPolicy: keepSource` and both post-leg
   VolSync updates/syncs. B18a gates rebuild; B18b follows creation of
   `lvms-knowhere` and gates production leg 2. All remain pending; no halted-
   production test or V03 restore can replace them.
7. **SELinux/sparse support:** prove encrypted archive extraction preserves
   numeric owners, ACLs/xattrs and sparse allocation, and that final-namespace
   volume relabeling leaves each workload able to read its data. A scratch
   namespace has a different MCS label and must not be compared byte-for-byte for
   SELinux context.
8. **Live-migration and xandar details:** the installed v1alpha1 plan/migration
   schemas and `rootdisk`/`projectsdisk` names are confirmed; recheck them and
   controller behavior for drift. Apply and prove the declared `knowhere-lan`
   mapping on xandar before leg 1, confirm destination capacity, select the
   interim non-vg1 VolSync cache class, and record GitOps freeze/reconciliation
   owners. Direct/same-node migration is not a fallback.
9. **Observation and teardown:** approve the interim-iSCSI observation duration
   and the later review criteria for old vg1 source deletion and interim teardown.
   Neither successful migration nor completed status authorizes source deletion.
10. **Current-state drift:** this is a point-in-time inventory. Any new/missing
    PVC, changed size/mode/class, changed ReplicationSource, new writer, or changed
    allocated-data budget requires a reviewed update, not an ad-hoc decision.
