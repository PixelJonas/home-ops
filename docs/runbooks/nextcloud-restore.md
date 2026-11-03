# Nextcloud Restore Runbook

Full disaster-recovery restore (namespace/PVCs destroyed or corrupted).
For a live-cluster restore, skip steps that create objects which already
exist.

See also: [volsync-restore.md](volsync-restore.md) (generic RD restore and
verification), [database-restore.md](database-restore.md) (dump transfer,
replay and checks) and [storage-rebuild-playbook.md](storage-rebuild-playbook.md)
(restoring many apps at once).

Resource names below are confirmed against the live Altus cluster
(2026-08-09), not guessed from Helm-chart conventions:

| Resource | Name |
|---|---|
| Namespace | `nextcloud` |
| ArgoCD leaf Application | `nextcloud-app` |
| ArgoCD parent (app-of-apps) Application | `cluster-config-manager` |
| Nextcloud Deployment | `nextcloud-app` |
| CNPG Cluster (PostgreSQL) | `nextcloud-db` (Service `nextcloud-db-rw`) |
| Database / owner | `nextcloud` / `nextcloud` |
| Owner credentials Secret | `nextcloud-db-nextcloud-secret` (basic-auth, keys `username`, `password`) |
| Backup CronJob | `database-psql-backup` (`templates/psql-backup`, patched in `components-apps/nextcloud/backup/database/`) |
| App-data PVC (webroot/config/custom_apps/themes) | `nextcloud-app-nextcloud` |
| User-file-data PVC (`nextcloud.datadir`, `synology-nfs-storage`) | `nextcloud-app-nextcloud-data` |
| User-file-data local restic-rest repo secret | `restic-config-data-nfs` |
| Database-dump staging PVC | `database-syno-db-backup` |
| Local restic-rest repo secrets | `restic-config-database`, `restic-config-data` |
| Backblaze B2 repo secrets | `restic-config-database-b2`, `restic-config-data-b2` |

> **Backup coverage note (as of 2026-08-09):** `nextcloud-app-nextcloud-data`
> was added later than this runbook's original backup setup, when user
> file storage was split onto its own `synology-nfs-storage` PVC (see
> `components-apps/nextcloud/nextcloud-app.yaml`'s `persistence.nextcloudData`
> block). It now has its own **local-only** `ReplicationSource`
> (`components-apps/nextcloud/backup/data-nfs/`, repo secret
> `restic-config-data-nfs`, daily at 03:15) — verified live, 470 files /
> 262 MiB backed up successfully. **No B2 (off-cluster) leg for this
> PVC, by deliberate decision** (this storage is expected to grow into
> real photo/file volume, and duplicating that to B2 on top of the
> existing local + tailsafe off-site copies wasn't judged worth the
> storage cost). Step 5a below restores from the local repo only — this
> protects against a PVC-level issue and, via tailsafe's existing
> `cluster-backups` mount on the same restic-rest-server storage, a
> single-NAS-drive failure, but not a full Synology NAS loss. Revisit
> with a B2 `ReplicationSource` (mirroring `backblaze.yaml` in the
> `data/` directory) if that trade-off changes.
>
> **NFS root_squash caution, if/when restoring this PVC**: this PVC's
> underlying Synology export uses `root_squash`, which silently remaps any
> uid-0 (root) client to a restricted identity that can't `chown`/`chmod`
> arbitrarily. If you extract/restore into this PVC via a debug pod running
> as root (the default for `oc exec`/most debug-pod specs), files land
> owned by the wrong identity and the *actual* Apache worker process (which
> runs as uid 33/`www-data`, not root) will fail to read them — this caused
> a real outage during the initial migration. Always run the restore
> container with `securityContext: { runAsUser: 33, runAsGroup: 33 }`, not
> as root. See `.claude/rules/synology.md` in `infra-ops` for the full
> writeup.

## Backup consistency: what the maintenance-mode window actually guarantees

The `database-psql-backup` CronJob (`templates/psql-backup/cronjob.yaml`, with the `SYNC_DIR` handshake and the `maintenance-mode` container added by `components-apps/nextcloud/backup/database/cron.yaml`) puts
Nextcloud into maintenance mode before the DB dump and keeps it there via a
fixed hold afterward, specifically so the DB dump and the app-data PVC
backup are taken from a coordinated window rather than two independently-
scheduled, non-overlapping points in time. Concretely:

- The DB dump CronJob runs at **03:00** daily.
- The app-data `ReplicationSource`s (both `data-backup` local and
  `data-backup-b2`, see `components-apps/nextcloud/backup/data/local.yaml`
  and `backblaze.yaml`) were retimed from this fleet's `templates/volsync/*`
  default of 05:00 to **03:10** — 10 minutes after the dump starts —
  specifically so their trigger falls inside the maintenance-mode window
  below instead of two hours after it closes.
- Maintenance mode stays on for a fixed **~12 minutes** (720s) from the
  CronJob's start, computed relative to the job's own start timestamp (not
  a flat sleep tacked onto the end), so a slower-than-expected dump doesn't
  push the total window out indefinitely. That comfortably covers a small
  (2-user) `pg_dumpall` (expected well under a minute) plus the 03:10
  data-backup trigger, with roughly a 2-minute safety margin for normal
  CronJob/VolSync scheduling latency.

How the Job does it (since the dump moved off `kubectl exec`): the pod has
two containers. `pg-dump` runs the shared `psql-backup` pipeline: it checks free
space and the server/client versions first, signals `preflight-ok`, waits for
maintenance mode (exit 69 if it could not be enabled), then runs `pg_dumpall`
against the `nextcloud-db-rw` Service, validates the result structurally (cluster
header and footer, UTF-8, COPY blocks terminated with matching field counts, see
[database-restore.md](database-restore.md)) and renames it to `backup.sql`; it
writes its exit code to `/sync/dump-done` on exit. `maintenance-mode` is the only part
that uses the Kubernetes API: it enables maintenance mode only after the dump
preflight passed, holds it as described above after a successful dump, and
switches it off at once (no hold) if the dump failed or the pod is stopped.

**The actual guarantee: both backups are triggered inside one shared,
coordinated ~12-minute nightly maintenance window (03:00-03:12), instead of
the DB dump's window closing within seconds while the data backup's own
schedule left a ~2-hour gap where the two were never point-in-time
consistent with each other at all.**

**What this does NOT guarantee:** the app-data `ReplicationSource` uses
`copyMethod: Direct` (no snapshot/clone step — restic reads straight off
the live PVC for the duration of its own backup run), the same pattern
every other Direct-copyMethod app in this fleet already uses (immich,
paperless). For a very large app-data PVC, the restic transfer itself
could still be running after the 03:12 maintenance-mode hold ends — this
fix closes the previous multi-hour non-overlap gap, it does not add
sub-second atomicity beyond what `copyMethod: Direct` already provides
fleet-wide. If the app-data PVC grows large enough that this becomes a
real concern, revisit with either a longer hold or `copyMethod: Snapshot`
(a genuine point-in-time clone) for this component specifically.

1. **Disable ArgoCD auto-sync on both Applications** — parent
   (`cluster-config-manager`) first, then the `nextcloud-app` Application
   itself. Patch order matters: disabling the child before the parent lets
   the parent's next reconcile silently revert the child's patch.

   ```bash
   oc patch application cluster-config-manager -n openshift-gitops --type json \
     -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
   oc patch application nextcloud-app -n openshift-gitops --type json \
     -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
   ```

2. **Confirm restic-config Secrets exist in the `nextcloud` namespace.**
   If restoring into a namespace ArgoCD hasn't reconciled yet (true
   disaster recovery), the `ExternalSecret`s created by this backup setup
   (`database-external-restic-config-b2`, `data-external-restic-config-b2`,
   etc.) create `restic-config-database-b2` / `restic-config-data-b2`
   automatically once ArgoCD syncs the `components-apps/nextcloud/backup`
   manifests — sync just those objects, or `oc apply` them directly as a
   stopgap, before proceeding.

3. **Scale the app to zero** (nothing may write to the DB during the load):

   ```bash
   oc scale deployment/nextcloud-app --replicas=0 -n nextcloud
   ```
   (The `nextcloud-db` Cluster stays running; step 6 replays into it.)

4. **Restore the database-dump PVC** from B2 (or the local REST tier if
   Altus itself is intact and it's just data corruption, not a full
   disaster):

   ```bash
   cat <<EOF | oc apply -f -
   apiVersion: volsync.backube/v1alpha1
   kind: ReplicationDestination
   metadata:
     name: nextcloud-database-restore
     namespace: nextcloud
   spec:
     trigger:
       manual: restore-once
     restic:
       repository: restic-config-database-b2
       destinationPVC: database-syno-db-backup
       copyMethod: Direct
   EOF
   ```

   Wait for completion:

   ```bash
   oc wait --for=condition=Synchronizing=false replicationdestination/nextcloud-database-restore \
     -n nextcloud --timeout=15m
   ```

5. **Restore the app-data PVC**, same shape, different names:

   ```bash
   cat <<EOF | oc apply -f -
   apiVersion: volsync.backube/v1alpha1
   kind: ReplicationDestination
   metadata:
     name: nextcloud-data-restore
     namespace: nextcloud
   spec:
     trigger:
       manual: restore-once
     restic:
       repository: restic-config-data-b2
       destinationPVC: nextcloud-app-nextcloud
       copyMethod: Direct
   EOF
   ```

   ```bash
   oc wait --for=condition=Synchronizing=false replicationdestination/nextcloud-data-restore \
     -n nextcloud --timeout=15m
   ```

5a. **Restore the user-file-data PVC** (`nextcloud-app-nextcloud-data`)
    from the local repo, same shape as step 5, different PVC/repo name.
    The mover needs the same `moverSecurityContext` the backup itself uses
    (uid/gid 33) — see the `root_squash` caution below for why:

    ```bash
    cat <<EOF | oc apply -f -
    apiVersion: volsync.backube/v1alpha1
    kind: ReplicationDestination
    metadata:
      name: nextcloud-data-nfs-restore
      namespace: nextcloud
    spec:
      trigger:
        manual: restore-once
      restic:
        repository: restic-config-data-nfs
        destinationPVC: nextcloud-app-nextcloud-data
        copyMethod: Direct
        moverSecurityContext:
          runAsUser: 33
          runAsGroup: 33
          runAsNonRoot: true
    EOF
    ```

    (No B2 leg exists for this PVC yet — the above only protects against a
    PVC-level issue, not a full Synology NAS loss. See the backup coverage
    note above.)

    If instead you need to manually extract/restore into this PVC (e.g. via
    a debug pod and a raw tarball, not VolSync) — **run the debug pod as
    uid 33/`www-data`, never as root**, per the `root_squash` caution
    above. A plain `oc exec ... tar xzf ...` as root will silently produce
    files the real app can't read.

6. **Load the restored dump into PostgreSQL (`nextcloud-db`).** The dump is a
   `pg_dumpall` of the whole cluster, replayed as the superuser into an empty
   `nextcloud` database. [database-restore.md](database-restore.md) sections
   2-5 describe the generic procedure; the Nextcloud-specific values are below.
   **Never pipe the dump from one `oc exec` into `psql`**: the exec stream can
   end early without an error and the database is then silently missing
   everything after the cut.

   a. Nextcloud stays at 0 replicas (step 3). Make sure the Cluster is healthy
      and resolve the primary:

      ```bash
      oc wait --for=condition=Ready cluster.postgresql.cnpg.io/nextcloud-db -n nextcloud --timeout=10m
      PRIMARY="$(oc get cluster.postgresql.cnpg.io nextcloud-db -n nextcloud -o jsonpath='{.status.currentPrimary}')"
      ```

   b. Force a clean target (CNPG bootstrap can pre-create the schema, and the
      dump's `CREATE DATABASE` would then no-op against it). Only on a freshly
      initialized, empty database, never on one holding data you have not
      backed up:

      ```bash
      oc exec -n nextcloud "$PRIMARY" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
        -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'nextcloud' AND pid <> pg_backend_pid();" \
        -c "DROP DATABASE IF EXISTS nextcloud;" \
        -c "CREATE DATABASE nextcloud OWNER nextcloud ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;"
      ```

   c. Mount the restored dump PVC in a one-shot debug pod (it is ReadWriteOnce,
      so it cannot be shared with a running pod), copy the dump into the
      database pod, prove the copy by sha256, and replay from that local file:

      ```bash
      cat <<EOF | oc apply -f -
      apiVersion: v1
      kind: Pod
      metadata:
        name: nextcloud-restore-debug
        namespace: nextcloud
      spec:
        restartPolicy: Never
        containers:
          - name: debug
            image: quay.io/openshift/origin-cli:4.22
            command: ["sleep", "3600"]
            volumeMounts:
              - name: backup
                mountPath: /backup
        volumes:
          - name: backup
            persistentVolumeClaim:
              claimName: database-syno-db-backup
      EOF
      oc wait --for=condition=Ready pod/nextcloud-restore-debug -n nextcloud --timeout=5m

      # 1. Hash at the source. Never cat the dump to your terminal.
      oc exec nextcloud-restore-debug -n nextcloud -- sha256sum /backup/backup.sql

      # 2. Copy into the database pod (check `df -h /tmp` there first; use a
      #    path on the data volume if /tmp is too small, and delete it after).
      oc exec nextcloud-restore-debug -n nextcloud -- cat /backup/backup.sql | \
        oc exec -i -n nextcloud "$PRIMARY" -c postgres -- sh -c 'cat > /tmp/restore.sql'

      # 3. Hash at the target. It must equal step 1; if not, repeat step 2.
      oc exec -n nextcloud "$PRIMARY" -c postgres -- sh -c 'stat -c %s /tmp/restore.sql; sha256sum /tmp/restore.sql'

      # 4. Replay from the local file, then review the log.
      oc exec -n nextcloud "$PRIMARY" -c postgres -- sh -c \
        'psql -U postgres -d postgres -f /tmp/restore.sql > /tmp/restore.log 2>&1; echo "rc=$?"'
      oc exec -n nextcloud "$PRIMARY" -c postgres -- sh -c \
        'grep -c "ERROR" /tmp/restore.log; grep "ERROR" /tmp/restore.log | sed -E "s/^psql:[^ ]*: //; s/\"[^\"]*\"/\"<id>\"/g" | sort | uniq -c'

      # 5. Sanity counts, then remove the copy and the debug pod.
      oc exec -n nextcloud "$PRIMARY" -c postgres -- psql -U postgres -d nextcloud -Atc \
        "SELECT count(*) FROM oc_users; SELECT count(*) FROM oc_calendarobjects;"
      oc exec -n nextcloud "$PRIMARY" -c postgres -- rm -f /tmp/restore.sql /tmp/restore.log
      oc delete pod nextcloud-restore-debug -n nextcloud
      ```

      `pg_dumpall` replays roles and databases that already exist on a CNPG
      target, so a few errors are expected (`role "postgres" already exists`,
      the `nextcloud` role, `database "nextcloud" already exists` if you
      skipped 6b). Any other error class (relation, extension, data,
      permission) fails the restore; see database-restore.md section 4. The
      replay must reach the end of the dump (last COPY applied), and the counts
      must match the values recorded at backup time. The dump omits role
      passwords (`--no-role-passwords`); the owner password stays in
      `nextcloud-db-nextcloud-secret`, which ESO and CNPG manage.

7. **Scale Nextcloud back up:**

   ```bash
   oc scale deployment/nextcloud-app --replicas=1 -n nextcloud
   ```

8. **Verify:**
   - Web login works for both `jonas` and `julia`.
   - The shared calendar is present with its events.
   - Contacts are present.
   - `oc exec deploy/nextcloud-app -n nextcloud --container nextcloud -- php occ status`
     reports `installed: true`, no maintenance mode stuck on. (Note: the
     `occ` script is not on `$PATH` in the official Nextcloud image —
     always invoke it as `php occ`, run from the container's default
     working directory `/var/www/html`.)

9. **Re-enable ArgoCD auto-sync** on both Applications (order-independent
   for re-enabling, unlike disabling):

   ```bash
   oc patch application nextcloud-app -n openshift-gitops --type merge \
     -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
   oc patch application cluster-config-manager -n openshift-gitops --type merge \
     -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
   ```

10. **Delete temp resources:**

    ```bash
    oc delete replicationdestination nextcloud-database-restore nextcloud-data-restore -n nextcloud
    # delete any throwaway debug pod from step 6c if it wasn't already removed
    oc delete pod nextcloud-restore-debug -n nextcloud --ignore-not-found
    ```

## Startup order in a mass restore

When Nextcloud is restored as part of a larger recovery (see
`storage-rebuild-playbook.md`), keep maintenance mode **on** from the final
dump until the household has accepted the restore, and make sure no scheduled
job (including the `database-psql-backup` CronJob, which toggles maintenance mode)
can turn it off early. Order:

1. PostgreSQL (`nextcloud-db`) alone; load and verify the dump (step 6).
2. Redis, empty (it is a cache; nothing is restored into it).
3. Nextcloud app with maintenance mode still on; `php occ status` must show
   `installed: true`.
4. Household check: web and sync-client login (with second factor), shared
   calendars and contacts, recent files, one download and one upload.
5. Maintenance mode off.
6. Metrics exporter, then the background-jobs CronJob and the backup CronJob
   (unsuspend), then a fresh post-restore backup of every ReplicationSource.

## Dry-run status

**Not yet performed, but no longer blocked on the B2 buckets.** As of
2026-08-09 both Backblaze B2 buckets (`volsync-nextcloud-database`,
`volsync-nextcloud-data`) exist and all four `ReplicationSource`s covering
the *original* two PVCs (`data-backup`/`data-backup-b2`/`database-backup`/
`database-backup-b2`) are confirmed `Successful` — a Doppler-side hostname
bug that was silently crash-looping the local (non-B2) `data-backup` leg
has since been found and fixed. This runbook's restore steps 1-4, 5, and
6-10 are ready to dry-run for real. Step 5a (the newer user-file-data PVC)
is still blocked — see the known-gap note in the resource table above; no
backup exists for that PVC yet, so there's nothing to restore in a dry run
until that's built.

Once at least one backup cycle has completed (after the first 03:00
`database-psql-backup` CronJob run followed by the 03:10 VolSync trigger),
dry-run this procedure — either into a scratch namespace
(`nextcloud-restore-test` with its own throwaway Postgres + the restored
PVCs, verify the dump loads and the schema looks sane via `\dt`
and a spot-check of the `oc_calendarobjects` row count, then delete the
scratch namespace), or, if willing to accept brief downtime, for real
against `nextcloud` during a low-traffic window (restoring the latest
backup onto itself is a no-op for data as long as no writes happened since
the last backup).

Record the actual wall-clock time the restore took (both PVC sizes will
still be small early on) as a baseline for future runs. Once the dry-run
succeeds, the design spec's success criterion ("A documented, tested
restore procedure exists and has been dry-run at least once") is satisfied
for this item — update this section with the date and outcome at that
point.
