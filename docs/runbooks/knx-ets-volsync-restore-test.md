# KNX ETS VM — VolSync backup restore test

Full end-to-end verification that the `knx-ets` VolSync backups
(`volume-backup`, `persistent-state-backup` → in-cluster restic-rest-server,
local-only, no B2) can actually rebuild the VM. Run quarterly and after CNV
upgrades. The production VM stays running throughout — everything happens in
a throwaway namespace. Budget ~45 min, mostly waiting on restic and Windows
boot.

References: `templates/replicationdestination.yaml`, `nextcloud-restore.md`
(generic restore procedure), infra-ops `docs/journal/knx-ets-vm-restore-2026-10-11.md`.

## Phase 0 — preflight

```bash
oc get replicationsource -n knx-ets          # both must show a recent LAST SYNC
oc get vm windows-11-virtio-ets -n knx-ets   # prod VM; leave it alone
oc debug node/altus -q -- chroot /host lvs -o lv_name,data_percent vg1 | grep thin-pool
# thin pool needs ~35Gi free for the restored copies (source is ~28.5Gi stored)
```

## Phase 1 — throwaway namespace + restic secrets

Create namespace `knx-ets-restore-test`. The ReplicationDestination reads its
restic credentials from a Secret named like the source's (`restic-config-volume`,
`restic-config-persistent-state`) **in the destination namespace**. Do NOT
`oc get secret -o yaml` the existing ones (that prints the restic password) —
re-materialize them via ad-hoc ExternalSecrets against the same Doppler keys:

```yaml
# one per volume: restic-config-volume, restic-config-persistent-state
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: <name>                     # external-restic-config-volume / -persistent-state
  namespace: knx-ets-restore-test
spec:
  secretStoreRef: {kind: ClusterSecretStore, name: doppler-cluster}
  target: {name: <restic-config-volume|restic-config-persistent-state>}
  data:
    - secretKey: RESTIC_REPOSITORY
      remoteRef: {key: <KNX_ETS_VOLUME_REST_REPO_LOCAL|KNX_ETS_PERSISTENT_STATE_REST_REPO_LOCAL>}
    - secretKey: RESTIC_PASSWORD
      remoteRef: {key: RESTIC_PASSWORD}
```

Verify `SecretSynced` on both before continuing.

## Phase 2 — destination PVCs

```yaml
# knx-ets-restore-test namespace
# volume-test:        64Gi,  lvms-vg1, RWO, volumeMode: Filesystem
# state-test:         300Mi, lvms-vg1, RWO, volumeMode: Filesystem,
#                     label persistent-state-for: windows-11-virtio-ets-test
```

The label value must match the **test** VM name — KubeVirt looks up the
persistent-state PVC by `persistent-state-for: <vm-name>` in the VM's
namespace.

## Phase 3 — restore with ReplicationDestination

One per PVC, `templates/replicationdestination.yaml` shape:

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: volume-restore           # and state-restore
  namespace: knx-ets-restore-test
spec:
  trigger: {manual: restore-once}
  restic:
    repository: restic-config-volume      # resp. restic-config-persistent-state
    destinationPVC: volume-test           # resp. state-test
    copyMethod: Direct                    # destination is not in use — Direct is correct here
```

Wait for `status.latestMoverStatus.result: Successful` on both (volume takes
~6-10 min based on the backup's 6m26s write).

## Phase 4 — data verification (before any boot)

Helper pod mounting both restored PVCs. Files are owned `107:107` with
mode 660, so the pod needs root: temporarily
`oc adm policy add-scc-to-user anyuid -z default -n knx-ets-restore-test`
(remove it in Phase 6).

Checks:
- `stat -c '%s %u:%g' /restore/root/disk.img` → `60040413184 107:107`
- `find /restore/state -name 'tpm2-00.permall'` →
  `swtpm/b73ae2a8-c599-45f0-9830-33e50088fae3/tpm2/tpm2-00.permall`
- `/restore/state/nvram/windows-11-virtio-ets_VARS.fd` present
- Optional repo integrity: `restic check --read-data-subset=5%` from a pod
  with the restic-config env (`image: restic/restic`).

## Phase 5 — boot test

Clone `components-infra/knx-ets/base/virtualmachine.yaml` into the test
namespace with exactly these changes:

- `metadata.name: windows-11-virtio-ets-test` (matches the state PVC label)
- **Delete** `devices.hostDevices` — the dongle is a singleton and belongs to
  the prod VM. ETS will run in demo mode; that is expected and fine.
- Change the interface `macAddress` (e.g. `02:e7:4f:bd:a6:38`) to avoid two
  live guests sharing a MAC. Windows may flag activation — cosmetic, discard
  with the namespace.
- **Keep** `firmware.serial` and `firmware.uuid` byte-identical — the
  restored vTPM state is keyed to the UUID; changing it triggers BitLocker
  recovery, which is precisely the failure this test watches for.
- Keep instancetype `o1.large`, preference `windows.11.virtio`, the
  reenlightenment override, machine type, and the virtio-win containerDisk.
- Volumes point at `volume-test` / nothing else changes; `runStrategy: Halted`.

Boot: `oc patch vm windows-11-virtio-ets-test -n knx-ets-restore-test --type merge -p '{"spec":{"runStrategy":"Always"}}'`

Success criteria:
- `vmi ... AgentConnected=True` within ~5 min (guest agent cannot run from a
  BitLocker recovery screen — connected agent proves the vTPM unlock worked)
- `status.guestOSInfo.prettyName` = Microsoft Windows 11
- virt-launcher on node `altus`

## Phase 6 — cleanup

```bash
oc patch vm windows-11-virtio-ets-test -n knx-ets-restore-test --type merge -p '{"spec":{"runStrategy":"Halted"}}'
oc adm policy remove-scc-from-user anyuid -z default -n knx-ets-restore-test
oc delete namespace knx-ets-restore-test
oc get replicationsource -n knx-ets   # prod: schedule intact, no manual trigger left
```

## Notes / risks

- Prod VM is never touched: restore destinations are separate PVCs, and
  `copyMethod: Direct` on the **destination** side does not involve the live
  disks. (The Direct-vs-Snapshot hazard is source-side only.)
- Two VMs with the same firmware UUID/serial can coexist — the value is
  guest-visible only. The MAC is the one that must differ.
- Expected test-only deviations: ETS6 unlicensed (no dongle), possible
  Windows activation notice (MAC change). Both vanish with the namespace.
- If the test VM lands in BitLocker recovery: vTPM state mismatch — check the
  state PVC label matches the test VM name and the firmware UUID is identical,
  then re-run Phase 4's `tpm2-00.permall` check.
- Until the first run of this runbook passes, the July 2026 NAS tars
  (`pmb-backup/knx-ets-knx-ets-backup/`) are the authoritative fallback —
  they are kept indefinitely (decision 2026-10-11).
