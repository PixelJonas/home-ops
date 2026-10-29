# Altus LVMS migration: backup and restore runbook

Date of read-only inventory: 2026-10-26

This is the backup/restore companion to the Altus LVMS rebuild and Knowhere
isolation plan. It covers every production PVC that used `lvms-vg1` at the
current inventory checkpoint. Knowhere uses the approved coordinated cold route:
Asgard is the single writer, the guest is gracefully stopped and both source
disks detach, fresh O01 qcow2-LUKS root and V03 projects backups are captured,
and paired cold B18C isolated restore/boot rehearsal proves the recovery set.
The old pool is rebuilt only under separate authorization. Both Knowhere disks
are then restored to `lvms-knowhere` under their original names, a final VolSync
backup succeeds, and exactly one fresh production boot is expected with CPU
model `Skylake-Client-noTSX-IBRS`.

The old pool must not be recreated until the cold O01+V03 backup set, B18C, and
every other preserved production row's fresh verified backup and required
restore test pass. Historical B18 live-migration attempts are retained below as
superseded evidence; they are not prerequisites or an alternate production
route.

**Preparation status:** no production backup jobs, restores, workload stops,
deletions, or live storage migrations were run while preparing the original
plan. Synthetic scratch helper tests were executed and cleaned up without
touching production PVCs. The commands below are instructions for separately
approved maintenance and test windows, not a record that those actions have
happened. The synthetic qcow2/GPG and libguestfs helper evidence passed. The
historical B18 attempts did not establish a production route and are superseded
by the pending cold B18C rehearsal.

## Non-negotiable safety rules

- Run final migration operations from outside Knowhere. Asgard is the single
  writer for the cold Knowhere route. Coordinate one graceful guest shutdown and
  agent/session stop; verify the VMI and both source attachments are absent
  before either backup helper starts. Do not start Knowhere again until its two
  final restored disks and GitOps/VolSync references are ready for the one
  expected fresh boot.
- Pause ArgoCD auto-sync on affected parents first and children second. Record
  every changed Application. Otherwise a scale-down, PVC, DataVolume, or helper
  can be reconciled away or recreated unexpectedly. Keep Knowhere GitOps frozen
  through shutdown, cold backup, separately authorized rebuild, final restore,
  CPU-model reconciliation, and final VolSync validation; reconcile deliberately
  before unfreezing.
- Never run a final backup against an active writer. Stop applications before
  their databases, halt KNX and Knowhere, and verify source volumes are detached
  before an offline copy. The only exception is the logical database dump itself,
  taken while its database is available and all application writers are stopped.
- O01 and V03 are one cold Knowhere recovery set. Record both recovery points,
  artifact/snapshot IDs, checksums and validation together; a root backup without
  its paired projects backup (or vice versa) does not satisfy Gate B or Gate C.
  No step may automatically delete a source PVC/PV/DataVolume. Any source or
  scratch deletion requires its own explicit, named authorization.
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
- Plaintext O01–O07 artifacts are prohibited. Mandatory O01 uses qcow2-LUKS;
  O02–O07 stream tar through GnuPG AES256 so plaintext never lands on NFS. Use
  separate Doppler-owned high-entropy passphrases, each mounted as a 0400 Secret
  file.
- Expose the detached production root block device only to the approved O01
  backup helper. B18C and later restore tests use new block/filesystem PVCs and
  no production VM network identity. Never attach a production source disk to a
  second VM.
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
the independent root passphrase for the mandatory cold O01 qcow2-LUKS backup and
restore. The earlier running-production safety copy used the same key, but is
historical supporting evidence rather than the final cold artifact. These are key
names only; never place either value in this file, a Pod argument, shell
environment dump, or the NFS evidence directory:

- `ALTUS_LVMS_MIGRATION_KNOWHERE_ROOT_LUKS_PASSPHRASE`
- `ALTUS_LVMS_MIGRATION_FILESYSTEM_GPG_PASSPHRASE`

On 2026-10-27 both values were created in **Doppler `homelab/home`**, which is
the project visible to the cluster's `doppler-cluster` store. Initial duplicate
entries in `infra-ops/prd` were created during project discovery and are retained
for a separate deletion review; they are not the ESO source of truth.

Materialize required values with ExternalSecrets; do not copy existing Secret
data. The Knowhere example targets the root-encryption Secret required for cold
O01 backup/restore, and the filesystem example is repeated in each O02–O07
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

## Exact 107-PVC production routing inventory

The current live inventory is **110 `lvms-vg1` PVCs: 107 production claims plus
three `gb18-knowhere` scratch claims**. Production route IDs are disjoint and
complete: **V01–V16 = 16 VolSync restores, D01–D11 = 11 logical database
dump/imports, O01–O07 = seven offline copies, R01–R67 = 67 recreations, and
I01–I06 = six managed image reimports**. The exact production total is
**V16 + D11 + O7 + R67 + I6 = 107**. Knowhere V03 and O01 form one mandatory cold
recovery set and restore to `lvms-knowhere` under their original names.

The separate scratch set is `gb18-knowhere/knowhere-rootdisk` (300Gi Block),
`gb18-knowhere/knowhere-projects` (136579960013 bytes Filesystem), and
`gb18-knowhere/volsync-dst-restore-v03-cache` (1Gi). These are retained evidence
from superseded B18 attempts, not production rows. Track and authorize their
cleanup separately; never count the two interim iSCSI scratch PVCs as LVMS or
production inventory.

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
# Expected at this checkpoint: 110 total = 107 production + 3 gb18-knowhere scratch.
# Do not proceed on count alone; reconcile production and scratch names separately.
```

### V: 16 production VolSync filesystem restores

Restore each production V source with `copyMethod: Direct` into a new, unmounted
destination PVC. “Local+B2” means run and verify both final backups; B2 is the
preferred disaster-recovery source, while either verified repository may be used
for this pool rebuild. V03 is both a B18C scratch restore and the production
projects restore after the separately authorized rebuild. Its final destination
is `lvms-knowhere`, original name `knowhere-projects`, exact request
136579960013 bytes, paired with O01 before the single fresh boot.

| ID | Namespace | Source PVC | Request | ReplicationSource / repository | Restore and ownership notes |
|---|---|---|---:|---|---|
| V01 | actual-budget | actual-budget-app-data | 5Gi | data-backup / restic-config-data; data-backup-b2 / restic-config-data-b2 | Local+B2; preserve application ownership, expected pod UID/GID/fsGroup 1001. |
| V02 | gitea | gitea-shared-storage | 20Gi | data-backup / restic-config-data | Local; coordinated with D01. Preserve existing numeric ownership; do not force 1000. |
| V03 | knowhere | knowhere-projects | 136579960013 (~127.19Gi) | projects-backup / restic-config-projects | **Cold production route:** fresh stopped-writer sync paired with O01; B18C isolated restore/boot, then final `lvms-knowhere` restore under original name; exact `/disk.img`, then `chown 107:107 /disk.img`. |
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
| V16 | happier | happier-app-data | 5Gi | data-backup / restic-config-data; data-backup-b2 / restic-config-data-b2 | Local+B2; preserve current application numeric ownership and validate existing data after restore. |

### D: 11 logical database dump/import PVCs

These are database data PVCs on `lvms-vg1`; their `database-syno-db-backup`
staging PVCs are not on this storage class and therefore are not extra rows in
the 107-PVC production inventory. Recreate each data PVC empty, restore the staging dump
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

### O: seven production offline copies

O01 is the mandatory Knowhere root production route, paired with V03. Capture it
only after Asgard has gracefully stopped the guest and both disks are detached.
B18C must restore and boot the O01+V03 set before the separately authorized
rebuild; final O01 restoration to `lvms-knowhere` uses the original name.

| ID | Namespace | Source PVC | Request / mode | Offline artifact and purpose |
|---|---|---|---|---|
| O01 | knowhere | knowhere-rootdisk | 322122547200 (300Gi), Block | **Cold production route:** sparse self-contained qcow2-LUKS on approved NFS; full source comparison, B18C isolated restore/boot with V03, then raw restore to original-name `knowhere-rootdisk` on `lvms-knowhere`. |
| O02 | openshift-image-registry | image-registry | 150Gi, Filesystem | GnuPG-AES256-encrypted numeric-owner/xattr/ACL/SELinux/sparse tar stream; preserve complete registry content. |
| O03 | openshift-monitoring | alertmanager-main-db-alertmanager-main-0 | 10Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve alerts, silences, and notification state. |
| O04 | openshift-monitoring | prometheus-k8s-db-prometheus-k8s-0 | 40Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve all platform Prometheus history; do not trim. |
| O05 | openshift-user-workload-monitoring | prometheus-user-workload-db-prometheus-user-workload-0 | 40Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve all UWM history; do not trim. |
| O06 | openshift-user-workload-monitoring | thanos-ruler-user-workload-data-thanos-ruler-user-workload-0 | 40Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve rule state and local data. |
| O07 | paperless-mcp | paperless-mcp-app | 1Gi, Filesystem | GnuPG-AES256 encrypted tar stream; preserve OAuth DCR clients, signing keys, and proxy sessions/state. |

### R: 67 recreations without data restore

Do not copy these claims. Recreate them from GitOps after the replacement
StorageClasses are Ready. The 50 VolSync cache claims are disposable mover
caches; use the intended destination cache class. Knowhere's cache finally uses
`lvms-knowhere`; audit that no other application's final cache lands there.

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
| R10 | knowhere | volsync-src-projects-backup-cache | 1Gi | VolSync cache; final `lvms-knowhere` |
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
| R66 | happier | volsync-src-data-backup-b2-cache | 1Gi | VolSync cache; persistent data is V16 |
| R67 | happier | volsync-src-data-backup-cache | 1Gi | VolSync cache; persistent data is V16 |

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
and each complete offline archive. Mandatory cold O01 requires ciphertext SHA-256
plus a complete `qemu-img compare` against its detached source and restored target.

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

- [x] Historical 2026-10-27 inventory reconciled by name to the then-current 104
      production route IDs; all source PVCs were Bound. This is a dated baseline,
      not the current 107-production/110-total inventory.
- [ ] Current inventory reconciled to all 107 production route IDs plus exactly
      three separate `gb18-knowhere` LVMS scratch claims, including Happier V16,
      R66 and R67. Any drift blocks execution.
- [x] Current PV/PVC/DataVolume/VM and workload metadata exported without Secrets;
      refresh for Happier and any later drift before the final window.
- [x] Baseline numeric ownership, file counts, sizes, and application-specific
      database table/row counts captured, including coordinated NFS/library/export data.
- [x] Approved NFS directory is writable by the all_squash identity 1024:100;
      current measurements remain within the approved 370.807GiB budget or the
      reviewed 604.357GiB worst-case full-root budget for mandatory O01.
- [x] Every required restic repository on `/volume1/configs` passed sequential
      reachability/latest-snapshot checks; the existing tailsafe container is
      running and mounts the local repository read-only. B00 integrity remains separate.
- [x] The migration directory's missing tailsafe coverage is covered by explicit
      operator NAS-only risk acceptance dated 2026-10-27; it is not independently
      protected.
- [x] Required ExternalSecrets are `SecretSynced`; mounted passphrase files are
      0400, nonempty, readable only by the helper, and never logged. The root LUKS
      Secret is required for the mandatory cold O01 backup and restore.
- [x] The pinned virt-launcher digest provides qemu-img 10.1, GNU tar with
      xattr/ACL/SELinux/sparse support, GnuPG AES256, and `sha256sum`; the separate
      pinned libguestfs helper passed synthetic read-only inspection and write
      denial using the environment and writable mounts recorded above.
- [ ] Knowhere cold runbook, graceful-stop ownership on Asgard, paired O01+V03
      recovery-set procedure, and B18C isolated boot are reviewed. Final GitOps
      selects `domain.cpu.model: Skylake-Client-noTSX-IBRS`; no compatibility
      label fabrication or live-migration API is involved. Exactly one fresh
      production boot is expected after both final restores and final VolSync.
- [x] Knowhere GitOps freeze/unfreeze owners and exact VM/DataVolume/PVC/VolSync
      reference changes are reviewed. Final VolSync uses original-name
      `knowhere-projects` and `lvms-knowhere` for snapshot/cache. Freeze covers
      the entire cold stop/backup/rebuild/restore window, not read-only baselines.
- [x] Encrypted off-node etcd backup completed under the separate storage plan;
      OpenShift 4.22.15 artifact and NAS checksum verified on 2026-10-27.
- [ ] No unresolved cluster degradation, CI activity, deployments, or active
      migration streams.

### Gate B: restore tests and cold Knowhere rehearsal

- [ ] Representative restic filesystem restores compared for content and ownership,
      including Happier V16 and its existing application state/read-write checks.
- [ ] All 11 database dump types imported into isolated instances/namespaces and
      checked with real table/row counts and extension/application checks.
- [ ] **Fresh cold O01+V03 set passed:** Asgard coordinated the graceful stop,
      VMI and both attachments were absent, O01 qcow2-LUKS check/full comparison/
      SHA-256 passed, and the paired V03 sync/current snapshot/full repository
      check plus project manifests passed. The earlier running-root safety test
      is historical supporting evidence, not a substitute.
- [ ] **B18C passed before rebuild:** both artifacts from the same cold set were
      restored to new scratch PVCs, root comparison/read-only inspection passed,
      projects `/disk.img` content/metadata and 107:107 ownership passed, and one
      isolated two-disk boot using `Skylake-Client-noTSX-IBRS` proved the guest
      root/projects mounts and read-only project checks without production identity.
- [ ] B18C evidence explicitly records that it does not authorize rebuild or
      deletion. Historical B18a/B18b live attempts are superseded evidence only;
      no Xandar, continuous-availability, storage-migration-plan, or interim-iSCSI
      gate applies to the cold route.
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
- [ ] Gate B evidence includes dates, snapshot/dump/artifact IDs, source and
      target PV names/classes, cold stop/detach and isolated-boot logs, command
      exit statuses, and operator sign-off. The synthetic helper tests are helper
      evidence only; fresh cold O01+V03 and B18C evidence are still pending.

## Final backup sequence (maintenance window)

### 1. Quiesce writers and prove detach

1. Pause CI and deployments. Disable affected ArgoCD auto-sync parent-first.
2. Scale non-Knowhere application Deployments/StatefulSets to zero in dependency
   order: application workers first, databases last. Keep a database available
   only for its final logical dump. Stop Happier before its final V16 syncs.
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
9. KNX: halt the VM and wait for its VMI to disappear. Never rely on a guest
   shutdown request alone.
10. Knowhere: from Asgard, stop agent/herdr sessions and gracefully shut down the
    guest. Wait for its VMI and both source attachments to disappear. Only then
    attach the root source to the O01 helper and run the final V03 sync. Keep the
    guest stopped through B18C, rebuild and both final restores.

Read-only proof before each offline helper starts:

```bash
oc-sa altus get pods -A -o wide | grep -E 'knowhere|windows-11|prometheus|alertmanager|thanos-ruler|image-registry|paperless-mcp'
oc-sa altus get volumeattachments.storage.k8s.io
oc-sa altus get vm,vmi -n knowhere
oc-sa altus get vm,vmi -n knx-ets
# The relevant writer and attachment must be absent. Inspect any match; do not
# treat an empty grep exit status as a failed preflight. For cold O01/V03, no
# Knowhere VMI or source attachment may remain before the helpers start.
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

## Knowhere 300Gi Block root: mandatory cold O01 qcow2-LUKS backup

This is the executable production root route and a Gate B/C requirement. From
Asgard, gracefully stop Knowhere and prove the VMI and both source attachments
are absent before starting the helper. Pair the resulting artifact with the fresh
V03 projects backup, then prove both together in B18C before requesting the
separately authorized rebuild.

### Mandatory stopped-writer backup

O01 is a raw 300Gi block PVC. Its backup is a self-contained qcow2-LUKS image
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
virtual size fails the mandatory cold backup. There is no filesystem ownership
to `chown` inside this raw block backup.

### B18C isolated root restore, paired with V03

Test the exact fresh cold O01 artifact in the B18C scratch namespace together
with its paired V03 projects restore. Use a **non-production target StorageClass
approved for the test**, with at least 300Gi virtual capacity and enough real
allocation. Do not use a running production VM, production DataVolume/PVC,
production MAC/network identity, or old block PV. Materialize
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
for the B18C inspection step. Its synthetic runtime test passed with the
direct-backend environment and writable mounts recorded above. Do not make scratch
sparse-file guarantees a substitute for content comparison. Record the restored
PV, virtual size, comparison exit status, inspected partitions/filesystems, and
known-file result. B18C closes only when this root restore and its paired V03
restore also pass the isolated two-disk boot with
`Skylake-Client-noTSX-IBRS`; root-only evidence is insufficient.

## Knowhere production execution: coordinated cold backup and restore

Gate A, all other required Gate B tests, the fresh cold O01+V03 set, and paired
B18C must pass before requesting the rebuild. The production VM stays stopped
from its graceful shutdown through both final restores and final VolSync. There
are no storage-migration plans, compute migrations, interim production PVCs, or
continuous-availability gates. Exactly one fresh production boot is expected.

Freeze Knowhere GitOps for the entire cold window. Record the original VM,
DataVolume/PVC, VolSync and guest/network identity before shutdown, then
reconcile changes deliberately so ArgoCD neither restarts the guest nor restores
stale storage references. Preserve VM volume names `rootdisk` and `projectsdisk`,
the `projectsdisk` serial, firmware UUID, cloud-init and network identity.

### 1. Asgard shutdown and fresh paired backup

1. From Asgard, coordinate the maintenance window as single writer. End
   agent/herdr sessions and gracefully shut down the guest. Require the VMI and
   both source VolumeAttachments to disappear; a guest shutdown request or a VM
   spec alone is not detach evidence.
2. Capture O01 from the detached raw root using the mandatory qcow2-LUKS helper
   above. Require clean check, no backing file, 300Gi virtual size, complete
   comparison, ciphertext SHA-256 and apparent/allocated size.
3. Trigger V03 `projects-backup` after detach with Snapshot copy mode. Require a
   fresh successful sync/current snapshot, full repository read-data check,
   `/disk.img` metadata and scoped project manifests. Record O01 and V03 IDs and
   recovery times as one cold set; do not mix recovery points.
4. Run paired B18C from the restore-test runbook against new scratch PVCs and an
   isolated VM with `Skylake-Client-noTSX-IBRS`. Full root comparison, read-only
   inspection, exact projects restore and one isolated two-disk boot must pass.
   The earlier running-root safety artifact and historical B18 attempts are
   supporting evidence only and cannot replace this fresh cold set or rehearsal.

### 2. Separately authorized rebuild

Only after Gate C's required backup/restore rows and B18C pass may the operator
request and then execute the separately authorized storage rebuild that creates
`lvms-knowhere`. Backup/test success is not rebuild or deletion approval. The
rebuild's reviewed teardown must explicitly name every old production and
scratch object authorized for deletion; no controller or runbook step deletes
sources automatically. Preserve all off-pool backups and recovery evidence.
Validate Block and Filesystem provisioning and the final snapshot class before
production restoration.

### 3. Restore both original-name disks and perform one fresh boot

1. Keep the production VM stopped. Restore O01 to a new RWO 300Gi Block PVC named
   `knowhere-rootdisk` on `lvms-knowhere`; run a complete comparison against the
   validated qcow2-LUKS artifact and read-only guest inspection.
2. Create V03's destination as RWO Filesystem on `lvms-knowhere` with original
   name `knowhere-projects` and exact request **136579960013 bytes**. Restore the
   paired snapshot with Direct, require exact `/disk.img`, project manifests and
   metadata, and correct only that file to 107:107. Both restores must finish and
   validate before boot.
3. Reconcile the original DataVolume/PVC names and VM volume references, and set
   `spec.template.spec.domain.cpu.model: Skylake-Client-noTSX-IBRS`. Confirm the
   VM is still stopped after GitOps reconciliation; the CPU-model change and one
   expected fresh boot are intentional parts of this cold route.
4. Point `projects-backup` `spec.sourcePVC` at original-name `knowhere-projects`;
   set `spec.restic.volumeSnapshotClassName` and
   `spec.restic.cacheStorageClassName`/R10 to `lvms-knowhere`. Keep Snapshot copy
   mode, trigger a fresh manual sync, require success/current snapshot/repository
   evidence, and remove only the manual trigger. Do not boot before this final
   backup succeeds.
5. Perform exactly one expected fresh production boot. Require healthy
   `knowhere-lan`, projects mount, root/projects I/O, project manifests and
   representative `git fsck`, networking, Tailscale/herdr, user units, real agent
   use, and `mise run knowhere:verify`. Record the new boot ID explicitly rather
   than expecting unchanged-boot or continuous-availability evidence.
6. Unfreeze only after VM, storage, final VolSync and guest evidence agree. Keep
   all old sources that survived the separately reviewed rebuild, O01/V03
   artifacts and evidence through the observation window. Any later old-source or
   historical scratch cleanup requires another explicit named authorization;
   restore, backup and successful boot never imply deletion approval.

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
  repository. The NFS user-data volume is not one of the 107 production pool PVCs but is
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

For R01–R67, let GitOps/VolSync/workload controllers create empty PVCs only after
both StorageClasses are Ready and test provisioning has passed. Do not restore
old DinD layers, runner caches, Redis state, model caches, or mover caches.
Confirm all 67 have an intentional empty successor and that new cache class
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

### Gate C: final cold backups and B18C complete; original pool still intact

Require one signed evidence row for every production V/D/O route:

- V01–V16: fresh successful sync for every listed repository, latest snapshot
  identity/time, repository check, restore-test reference and ownership baseline.
  V03 additionally requires its fresh stopped-writer cold pairing with O01 and
  paired B18C restore/boot evidence; it is a final production restore row.
- D01–D11: new dump Job, nonzero `backup.sql`, byte size and SHA-256, fresh
  dump-PVC sync(s), repository check, isolated import and row-count evidence.
- O01: Asgard graceful-stop and both-disk detach proof, self-contained sparse
  qcow2-LUKS with no backing file, clean check, full source comparison,
  ciphertext SHA-256, apparent/allocated size, and paired B18C evidence. O01 is
  mandatory to unlock the separately authorized rebuild.
- O02–O07: stopped/detached proof, GnuPG decrypt-and-tar-list success with
  `pipefail`, ciphertext SHA-256, apparent and allocated size, scratch extraction
  comparison, and ownership/SELinux evidence; no plaintext `.tar` on NFS.
- R01–R67 and I01–I06: explicitly reviewed as recreation/reimport; no backup
  artifact is required. Track the three `gb18-knowhere` LVMS scratch claims
  separately from these production rows.

Do not cross the storage point of no return with any missing or failed row.
Failure of the cold O01+V03 set or B18C stops the rebuild request. Passing Gate C
does not itself authorize rebuild or deletion; the storage rebuild and every
named source/scratch deletion require separate explicit approval.

### Post-migration application checks and closeout

- Reconcile all 107 original production names to their V/D/O/R/I successor and
  reconcile the three historical scratch claims separately. No active VM or
  workload may reference the previous VG generation. Any source PV retained for
  observation must be detached/unreferenced and does not count as active old-pool
  use; original vg1 sources are absent only after their explicitly reviewed
  rebuild teardown, never through automatic cleanup.
- Gitea: repository/file checks agree with D01; real CI job passes with DinD
  MTU/overlay2 checks.
- Happier: V16 content/metadata and existing application state agree; real
  read/write validation and fresh local+B2 post-restore backups pass.
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
- Knowhere: fresh cold O01+V03 and B18C passed before the separately authorized
  rebuild; both original-name disks are on `lvms-knowhere` with exact size/mode
  and validated content. CPU model is `Skylake-Client-noTSX-IBRS`; exactly one
  expected fresh production boot occurred after final VolSync. Healthy projects
  mount/network/Tailscale/herdr, real agent use and `mise run knowhere:verify`
  pass. Final GitOps/VolSync reference `knowhere-projects`; snapshot/cache classes
  are `lvms-knowhere`; a fresh final projects sync succeeded. KNX must not request
  BitLocker recovery and must retain its vTPM state.
- Audit every ReplicationSource for fresh schedules, no leftover manual trigger,
  successful post-migration backups, and intended cache StorageClasses.
- Keep all backup repositories and migration artifacts unchanged for at least
  seven healthy days. Do not prune recovery data or automatically delete source
  PVCs/PVs/DataVolumes. Original vg1 and historical scratch deletion each require
  explicit named authorization; record the rebuild teardown decision, any later
  cleanup decision, and the retained-source/scratch inventory in closeout.

## Unresolved safety questions / required Gate A–B decisions

1. **Migration-artifact independent protection:** the approved
   `/volume1/pmb-backup/altus-lvms-migration/` directory is not currently
   tailsafe-covered. Add and verify equivalent independent protection, or record
   explicit operator acceptance of the NAS-resident-only risk for the defined
   migration/retention window. Existing `/volume1/configs` tailsafe coverage does
   not protect the O01–O07 artifacts.
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
6. **Knowhere cold safety and B18C:** from Asgard, prove graceful stop and detach,
   capture fresh O01 qcow2-LUKS and paired V03 backups, then pass paired cold
   restore/read-only inspection/isolated boot with
   `Skylake-Client-noTSX-IBRS`. B18C gates the rebuild request; final original-name
   restores, final VolSync and one expected fresh production boot follow the
   separately authorized rebuild. Historical running-root and B18a evidence cannot
   replace the fresh cold set or B18C.
7. **SELinux/sparse support:** prove encrypted archive extraction preserves
   numeric owners, ACLs/xattrs and sparse allocation, and that final-namespace
   volume relabeling leaves each workload able to read its data. A scratch
   namespace has a different MCS label and must not be compared byte-for-byte for
   SELinux context.
8. **Cold identity and GitOps:** confirm `rootdisk`/`projectsdisk`, original
   DataVolume/PVC names, firmware/cloud-init/network identity and final CPU model.
   Keep GitOps frozen through the full cold window, validate scratch/final
   capacity and `lvms-knowhere` snapshot/cache selection, and prevent any boot
   before both restores and final VolSync pass.
9. **Rebuild, observation and teardown:** obtain separate explicit rebuild
   authorization and an exact named deletion allowlist before pool teardown.
   Define the observation period and later review criteria for any surviving old
   sources and historical B18 scratch. Neither B18C, restore, backup nor fresh-boot
   success authorizes deletion.
10. **Current-state drift:** this is a point-in-time inventory. Any new/missing
    PVC, changed size/mode/class, changed ReplicationSource, new writer, or changed
    allocated-data budget requires a reviewed update, not an ad-hoc decision.
