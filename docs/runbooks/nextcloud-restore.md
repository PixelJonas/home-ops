# Nextcloud Restore Runbook

Full disaster-recovery restore (namespace/PVCs destroyed or corrupted).
For a live-cluster restore, skip steps that create objects which already
exist.

Resource names below are confirmed against the live Altus cluster
(2026-08-09), not guessed from Helm-chart conventions:

| Resource | Name |
|---|---|
| Namespace | `nextcloud` |
| ArgoCD leaf Application | `nextcloud-app` |
| ArgoCD parent (app-of-apps) Application | `cluster-config-manager` |
| Nextcloud Deployment | `nextcloud-app` |
| MariaDB StatefulSet | `nextcloud-app-mariadb` |
| MariaDB pod | `nextcloud-app-mariadb-0` |
| MariaDB root-password Secret | `nextcloud-mariadb`, key `mariadb-root-password` |
| App-data PVC | `nextcloud-app-nextcloud` |
| Database-dump staging PVC | `database-syno-db-backup` |
| Local restic-rest repo secrets | `restic-config-database`, `restic-config-data` |
| Backblaze B2 repo secrets | `restic-config-database-b2`, `restic-config-data-b2` |

## Backup consistency: what the maintenance-mode window actually guarantees

The `mariadb-backup` CronJob (`templates/mariadb-backup/cronjob.yaml`) puts
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
  (2-user) `mariadb-dump` (expected well under a minute) plus the 03:10
  data-backup trigger, with roughly a 2-minute safety margin for normal
  CronJob/VolSync scheduling latency.

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

3. **Scale everything to zero**, in this order (app first, so nothing keeps
   writing to the DB while it's mid-shutdown, then the DB):

   ```bash
   oc scale deployment/nextcloud-app --replicas=0 -n nextcloud
   oc scale statefulset/nextcloud-app-mariadb --replicas=0 -n nextcloud
   ```

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

6. **Load the restored dump into MariaDB.** This is the step a raw-PVC
   snapshot approach wouldn't need (the DB directory would already *be*
   MariaDB's live state) — because this fleet uses a logical dump, the
   dump file has to be replayed back in explicitly:

   a. Scale MariaDB back up just enough to accept connections, but make
      sure Nextcloud itself stays at 0 replicas so nothing else writes to
      the DB during the load:

      ```bash
      oc scale statefulset/nextcloud-app-mariadb --replicas=1 -n nextcloud
      # wait for the pod to be Ready
      oc wait --for=condition=Ready pod/nextcloud-app-mariadb-0 -n nextcloud --timeout=5m
      ```

   b. Safety step, same lesson as the CNPG "empty-database race" gotcha in
      `infra-ops`'s `.claude/rules/kubernetes-argocd.md` even though CNPG
      itself doesn't apply here: a MariaDB StatefulSet's first-ever start
      can auto-create an empty `nextcloud` schema before this restore
      reaches it. The dump file's own `CREATE DATABASE` statements will
      then silently no-op against that pre-existing (if empty) schema.
      Force a clean target regardless — cheap and harmless if it genuinely
      is empty:

      **Password note (verified live against `nextcloud-app-mariadb-0` on
      2026-08-09):** unlike the `mariadb-backup` CronJob — which gets the
      password via an explicit `env: MARIADB_ROOT_PASSWORD` `secretKeyRef`
      injected from outside — the running MariaDB pod itself does **not**
      have a plain `MARIADB_ROOT_PASSWORD` env var set. The Bitnami MariaDB
      image instead only exposes `MARIADB_ROOT_PASSWORD_FILE` (a path to a
      file containing the password, mounted from the same
      `nextcloud-mariadb` Secret). The commands below read the password
      from that file via `cat` inside the `oc exec`, entirely server-side —
      the value is never printed to your terminal or this document:

      ```bash
      oc exec nextcloud-app-mariadb-0 -n nextcloud --container mariadb -- /bin/bash -c \
        'MYSQL_PWD="$(cat "$MARIADB_ROOT_PASSWORD_FILE")" mariadb -u root -e "DROP DATABASE IF EXISTS nextcloud;"'
      ```

   c. Mount the restored database-dump PVC into a throwaway debug pod
      (it's ReadWriteOnce so it can't be double-mounted into a running
      pod — use a one-shot debug pod instead), then stream the dump into
      MariaDB. The simplest path is a one-off Pod manifest mounting
      `database-syno-db-backup` at `/backup`:

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
            image: quay.io/openshift/origin-cli:4.21
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

      oc exec nextcloud-restore-debug -n nextcloud -- cat /backup/backup.sql | \
        oc exec -i nextcloud-app-mariadb-0 -n nextcloud --container mariadb -- /bin/bash -c \
        'MYSQL_PWD="$(cat "$MARIADB_ROOT_PASSWORD_FILE")" mariadb -u root'

      oc delete pod nextcloud-restore-debug -n nextcloud
      ```

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

## Dry-run status

**Not yet performed.** This runbook has not been executed end-to-end — it
is blocked on the two Backblaze B2 buckets this backup setup depends on
(`volsync-nextcloud-database`, `volsync-nextcloud-data`) not existing yet.
See the implementation report for what a human needs to do in the B2
console before a real backup cycle (and therefore a real restore dry-run)
can happen.

Once the buckets exist and at least one backup cycle has completed (after
the first 03:00 `mariadb-backup` CronJob run followed by the 05:00 VolSync
snapshot), dry-run this procedure — either into a scratch namespace
(`nextcloud-restore-test` with its own throwaway MariaDB + the restored
PVCs, verify the dump loads and the schema looks sane via `SHOW TABLES;`
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
