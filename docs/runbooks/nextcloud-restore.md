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

**What this does NOT guarantee:** the window only lines up the *capture*
times. The app-data `ReplicationSource`s (`data-backup`, `data-backup-b2`)
use `copyMethod: Snapshot` (`lvms-vg1` VolumeSnapshot at the 03:10 trigger,
confirmed live 2026-11-07), so their image is a genuine point-in-time clone
taken while maintenance mode is on. The dump itself is uploaded later: the
`database-backup`/`database-backup-b2` `ReplicationSource`s snapshot the
`database-syno-db-backup` PVC on their own schedule (05:00 UTC on
2026-11-07; see `docs/volsync-schedules.md` if that moved), so the dump of
night D only reaches restic around two hours after the app-data snapshot of
night D. The two halves land in **different restic repositories with
different snapshot times and no shared tag**. Pairing them is a manual step
of the restore, see the next section. The user-file PVC
(`nextcloud-app-nextcloud-data`, `data-nfs-backup` at 03:15, `Direct`) is
outside the window altogether.

## DB and config.php belong together (`secret`)

The database and `config/config.php` are one unit. Restore them from the
**same night**, never one of them alone and never a DB dump onto a freshly
generated `config.php`. `config.php` lives only on the app-data PVC
(`nextcloud-app-nextcloud`, subPath `config`, mounted at
`/var/www/html/config`); there is no copy in Git or Doppler, and the
Nextcloud image writes a brand-new one with a new random `secret` whenever
it finds the PVC empty and installs.

What in `config.php` the database depends on:

| Key | What depends on it | Effect of a mismatch |
|---|---|---|
| `secret` | `ICrypto` key for every value Nextcloud encrypts in the DB: `oc_appconfig` rows starting with `$AppConfigEncryption$` (2026-11-07: `notifications/webpush_vapid_privkey`, `contacts/federations_cache`), TOTP secrets (`oc_twofactor_totp_secrets`), files_external credentials (`oc_storages_credentials`), OAuth2 client secrets, app_api/webhook_listeners secrets. Also hashes and encrypts every row in `oc_authtoken` (sessions and app passwords; 13 app passwords on 2026-11-07). | `HMAC does not match` (HTTP 500 on WebDAV writes, shares, notifications; incident 2026-11-07), TOTP codes rejected, every app password and session invalid, external-storage mounts fail. |
| `passwordsalt` | Only legacy SHA1 user password hashes (current hashes are bcrypt/argon2 and self-contained). | Old-style hashes stop verifying; current ones are unaffected. |
| `instanceid` | Name of `appdata_<instanceid>` in the data dir (user-file PVC) and the cookie names. | App data (previews, theming, app caches) "disappears" and is rebuilt; sessions drop. |
| `version` | Must match the DB schema and the image tag of that night. | `needsDbUpgrade` or a refused downgrade. |
| `dbtype`/`dbhost`/`dbname`/`dbuser` | Must point at `pgsql` / `nextcloud-db-rw` / `nextcloud` (since 2026-11-03). `config.php.pre-pgsql` next to it still points at MariaDB: never restore that one as `config.php`. | Nextcloud talks to a database that no longer exists. |
| `dbpassword` | Comes from `nextcloud-db-nextcloud-secret` (ESO/CNPG), not from the backup; must equal the `POSTGRES_PASSWORD` env of the pod. | Login to Postgres fails. |
| `installed` | Must be `true` before the first start, or the entrypoint runs `maintenance:install` and wipes the data dir (infra-ops `kubernetes-argocd.md`, "Restoring into a fresh PVC"). | Data loss. |

Share passwords and 2FA backup codes are hashes, not `secret`-encrypted;
they survive a `secret` mismatch.

`secret` itself only changes when Nextcloud installs anew, so any
`config.php` from the same installation decrypts the DB. Same-night pairing
still matters for `version`, `instanceid`/app data and for not mixing app
state from two different days. The real danger is an app-data PVC that is
recreated empty (migration, storage-class change, lost PVC) and then
installed fresh: that is how the 2026-11-07 incident started (app-data PVC
moved to `lvms-vg1` in 61ad049 on 2026-08-09, new `config.php` and new
`secret` the same day, old encrypted rows stayed in the DB). Any move of
`nextcloud-app-nextcloud` must carry `config/config.php` over, restored from
the backup or copied, before Nextcloud first starts on the new PVC.

### Picking a matching pair of snapshots

Both halves are VolSync/restic with the same tier A retention
(`within: 7d`, 14 daily, 8 weekly, 12 monthly, 2 yearly), so a pair for
every kept night exists in both repos as long as both legs ran that night.
For a restore of night **D** (all times UTC):

1. List the snapshots of both repos as in
   [volsync-restore.md](volsync-restore.md) step 4 (local repos
   `restic-config-data` / `restic-config-database`, B2 repos
   `restic-config-data-b2` / `restic-config-database-b2`) and record the IDs.
2. App data: the snapshot taken at about **D 03:10**.
3. Database: the **first snapshot after D 03:00 and before D+1 03:00**
   (05:00 on 2026-11-07). It contains the dump the 03:00 CronJob of D wrote.
4. Pin each RD with `restoreAsOf` after its chosen snapshot and before that
   repo's next one (VolSync takes the newest snapshot at or before the time;
   e.g. data `D T03:20:00Z`, database `D T07:00:00Z`, which also covers the
   05:16/06:16 schedule proposed in PR #1211). Check the pick against the
   recorded snapshot ID in the mover log. **Never restore
   "latest" on both sides**: between 03:10 and the database snapshot of the
   same night, "latest" pairs tonight's `config.php` with last night's dump.
5. Before Nextcloud starts, check the pair (step 5b): `backup.sql` must
   carry an mtime of D 03:0x, `config.php` an mtime ≤ D 03:12. A
   `backup.sql` from an earlier night means the dump CronJob failed on D
   (the pipeline keeps the previous file and VolSync uploads it again):
   pick an older D where both match, or accept the gap knowingly.
6. Use the image tag that ran on D (`nextcloud-app.yaml` at the Git commit
   of that day) so `version` in `config.php` matches the code.

If `secret` cannot be recovered at all (app-data repos lost, DB survived),
see "Recovering from a `secret` mismatch" under the checklist below.

## Restore steps

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
   disaster). Steps 4 and 5 restore **one pair from the same night D**:
   pick both snapshots first (see "Picking a matching pair of snapshots"
   above) and set `restoreAsOf` in both RDs. The YAML below shows the
   database leg; without `restoreAsOf` VolSync restores the latest snapshot,
   which is only safe when both latest snapshots belong to the same night:

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
       # first database snapshot after D 03:00 (see the pairing section)
       restoreAsOf: "<D>T07:00:00Z"
   EOF
   ```

   Wait for completion:

   ```bash
   oc wait --for=condition=Synchronizing=false replicationdestination/nextcloud-database-restore \
     -n nextcloud --timeout=15m
   ```

5. **Restore the app-data PVC** (webroot, `config/config.php` with
   `secret`, `custom_apps`, `themes`), same shape, different names, pinned to
   the 03:10 snapshot of the **same night D** as step 4:

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
       # the D 03:10 snapshot (see the pairing section)
       restoreAsOf: "<D>T03:20:00Z"
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

5b. **Check that the restored dump and `config.php` are a pair** before
    anything loads or starts. Mount both restored PVCs read-only in a
    one-shot pod (Nextcloud is at 0 replicas, so the RWO PVCs are free) and
    print only timestamps, sizes and key names, never values:

    ```bash
    cat <<EOF | oc apply -f -
    apiVersion: v1
    kind: Pod
    metadata:
      name: nextcloud-pair-check
      namespace: nextcloud
    spec:
      restartPolicy: Never
      containers:
        - name: check
          image: docker.io/library/nextcloud:<tag that ran on D>
          command: ["sleep", "1800"]
          volumeMounts:
            - {name: dump, mountPath: /dump, readOnly: true}
            - {name: app, mountPath: /app, readOnly: true}
      volumes:
        - name: dump
          persistentVolumeClaim: {claimName: database-syno-db-backup}
        - name: app
          persistentVolumeClaim: {claimName: nextcloud-app-nextcloud}
    EOF
    oc wait --for=condition=Ready pod/nextcloud-pair-check -n nextcloud --timeout=5m

    # Both must point at night D: backup.sql ~D 03:0x, config.php <= D 03:12.
    oc exec -n nextcloud nextcloud-pair-check -- \
      stat -c '%y %s %n' /dump/backup.sql /app/config/config.php

    # Keys present, correct DB wiring, installed flag (prints no secrets).
    oc exec -n nextcloud nextcloud-pair-check -- php -r '
      include "/app/config/config.php";
      foreach (["secret","passwordsalt","instanceid"] as $k)
        echo $k, " present=", var_export(!empty($CONFIG[$k]), true), "\n";
      foreach (["installed","version","dbtype","dbhost","dbname","dbuser"] as $k)
        echo $k, "=", var_export($CONFIG[$k] ?? null, true), "\n";'
    oc delete pod nextcloud-pair-check -n nextcloud
    ```

    Expected: `secret`/`passwordsalt`/`instanceid` present, `installed=true`,
    `dbtype='pgsql'`, `dbhost='nextcloud-db-rw'`, `dbname='nextcloud'`,
    `version` equal to the image of night D. If the timestamps belong to
    different nights, go back to steps 4/5 with a matching pair. After step 6
    the "encrypted values readable" check below is the real proof that
    `secret` and the database match.

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

8. **Verify:** run the full [post-restore checklist](#post-restore-checklist)
   below. In short:
   - Web login works for both `jonas` and `julia`, including the TOTP second
     factor.
   - The shared calendar is present with its events.
   - Contacts are present.
   - `oc exec deploy/nextcloud-app -n nextcloud --container nextcloud -- php occ status`
     reports `installed: true`, no maintenance mode stuck on. (Note: the
     `occ` script is not on `$PATH` in the official Nextcloud image —
     always invoke it as `php occ`, run from the container's default
     working directory `/var/www/html`.)
   - Encrypted values decrypt, WebDAV writes and a share work.

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
    oc delete pod nextcloud-restore-debug nextcloud-pair-check -n nextcloud --ignore-not-found
    ```

## Post-restore checklist

Run this after every restore (and after any event that may have recreated
`config.php`, e.g. an empty app-data PVC). Nextcloud should still be in
maintenance mode for the household (see "Startup order" below) except for
items 4-6, which need it off for the duration of the test. `occ` always runs
as `www-data`; no command below prints a secret.

```bash
occ() { oc exec -n nextcloud deploy/nextcloud-app -c nextcloud -- \
          su -s /bin/sh www-data -c "php occ $*"; }
```

1. **Status and version.** `occ status`: `installed: true`,
   `needsDbUpgrade: false`, `version` equal to the image of night D.
   `occ maintenance:data-fingerprint` once, so sync clients notice the
   restore and re-check their local state instead of uploading stale files
   as new.

2. **Encrypted values are readable (`secret` matches the DB).** This is the
   check that would have caught the 2026-11-07 incident. It tries to decrypt
   every `secret`-encrypted value and prints only names and OK/FAIL:

   ```bash
   cat > /tmp/nc-crypto-check.php <<'PHP'
   <?php
   // Read-only: decrypt every secret-encrypted value, print names + OK/FAIL only.
   require "/var/www/html/lib/base.php";
   $crypto = \OC::$server->get(\OCP\Security\ICrypto::class);
   $db = \OC::$server->get(\OCP\IDBConnection::class);
   $fail = 0;
   $try = function (string $label, string $cipher) use ($crypto, &$fail) {
       try { $crypto->decrypt($cipher); $s = "OK"; } catch (\Throwable $e) { $s = "FAIL"; $fail++; }
       echo $label, " ", $s, "\n";
   };
   $p = '$AppConfigEncryption$';
   $r = $db->executeQuery("SELECT appid, configkey, configvalue FROM *PREFIX*appconfig WHERE configvalue LIKE ?", [$p . "%"]);
   while ($row = $r->fetch()) { $try("appconfig " . $row["appid"] . "/" . $row["configkey"], substr($row["configvalue"], strlen($p))); }
   $r = $db->executeQuery("SELECT user_id, secret FROM *PREFIX*twofactor_totp_secrets");
   while ($row = $r->fetch()) { $try("totp user#" . substr(sha1($row["user_id"]), 0, 8), $row["secret"]); }
   $r = $db->executeQuery("SELECT id, credentials FROM *PREFIX*storages_credentials");
   while ($row = $r->fetch()) { if ((string) $row["credentials"] !== "") { $try("storages_credentials id=" . $row["id"], $row["credentials"]); } }
   echo "FAILED=", $fail, "\n";
   exit($fail > 0 ? 1 : 0);
   PHP
   oc exec -i -n nextcloud deploy/nextcloud-app -c nextcloud -- su -s /bin/sh www-data -c \
     'cat > /tmp/nc-crypto-check.php && php /tmp/nc-crypto-check.php; rc=$?; rm -f /tmp/nc-crypto-check.php; echo rc=$rc' \
     < /tmp/nc-crypto-check.php
   rm -f /tmp/nc-crypto-check.php
   ```

   Expected: every line `OK`, `FAILED=0`, `rc=0` (2026-11-07 baseline: two
   appconfig rows and two TOTP secrets, all OK). Any `FAIL` means `config.php`
   and the database come from different installations or nights: stop, go
   back to the pairing section. Also note the count of
   `oc exec -n nextcloud deploy/nextcloud-app -c nextcloud -- su -s /bin/sh www-data -c 'grep -c "HMAC does not match" /var/www/html/data/nextcloud.log'`
   (the data dir is not readable as root on the NFS PVC); it must not grow
   while you run the tests below.

3. **Logins and tokens.** Web login with password and TOTP code for both
   users. An existing app password still works (it is stored hashed and
   encrypted with `secret` in `oc_authtoken`): a `PROPFIND` with one of
   them returns `207`, e.g. the Home Assistant CalDAV or contacts sync
   password from Doppler, read into a netrc file as in the Secrets rules,
   never onto the command line.

4. **WebDAV write test (MKCOL/PUT/DELETE).** With an app password of a real
   account in a netrc file (`machine nextcloud.janz.digital login <user>
   password <app password>`, mode 600) and `U=<user>`:

   ```bash
   B="https://nextcloud.janz.digital/remote.php/dav/files/$U"
   T="restore-check-$(date +%s)"
   curl -s -o /dev/null -w 'MKCOL  %{http_code}\n' --netrc-file "$NETRC" -X MKCOL "$B/$T"
   echo restore-check | curl -s -o /dev/null -w 'PUT    %{http_code}\n' --netrc-file "$NETRC" -T - "$B/$T/probe.txt"
   curl -s --netrc-file "$NETRC" "$B/$T/probe.txt"
   curl -s -o /dev/null -w 'MOVE   %{http_code}\n' --netrc-file "$NETRC" -X MOVE \
     -H "Destination: $B/$T/probe-moved.txt" "$B/$T/probe.txt"
   curl -s -o /dev/null -w 'DELETE %{http_code}\n' --netrc-file "$NETRC" -X DELETE "$B/$T"
   ```

   Expected: `MKCOL 201`, `PUT 201`, the content `restore-check`,
   `MOVE 201`, `DELETE 204`. A `500` on any write is the `HMAC does not
   match` pattern (check item 2 and the log). Empty the test folder from the
   trash bin afterwards if you want no trace.

5. **Share test.** Create a password-protected public link on a test file,
   open it, delete the share (OCS API, same netrc):

   ```bash
   echo share-check | curl -s -o /dev/null --netrc-file "$NETRC" -T - "$B/$T-share.txt"
   SP="$(openssl rand -hex 12)"   # throwaway, never reused
   SID="$(curl -s --netrc-file "$NETRC" -H 'OCS-APIRequest: true' \
     -X POST 'https://nextcloud.janz.digital/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json' \
     --data-urlencode "path=/$T-share.txt" -d shareType=3 --data-urlencode "password=$SP" \
     | jq -r '.ocs.meta.statuscode, .ocs.data.id' | paste -sd' ')"
   echo "create: $SID"           # expect "200 <id>"
   curl -s -o /dev/null -w 'DELETE share %{http_code}\n' --netrc-file "$NETRC" -H 'OCS-APIRequest: true' \
     -X DELETE "https://nextcloud.janz.digital/ocs/v2.php/apps/files_sharing/api/v1/shares/${SID#* }"
   curl -s -o /dev/null -w 'DELETE file %{http_code}\n' --netrc-file "$NETRC" -X DELETE "$B/$T-share.txt"
   unset SP
   ```

   Expected: `create: 200 <id>`, `DELETE share 200`, `DELETE file 204`.
   Then check existing shares are still listed (`occ share:list`, count
   equal to the value recorded at backup time) and, in the browser, that a
   user share between `jonas` and `julia` (the shared calendar) still opens.

6. **Notifications / push.** `occ config:app:get notifications
   webpush_vapid_pubkey` returns a value (do not print the privkey) and the
   web UI's notification bell loads without a 500.

7. **Data dir matches the DB.** The user-file PVC is backed up at 03:15
   (`Direct`), not inside the window. If it was restored from a different
   point than the DB, run `occ files:scan --all` and
   `occ files:cleanup` so the file cache matches what is on disk; check
   that `appdata_<instanceid>` exists for the `instanceid` in `config.php`
   (`ls -d /var/www/html/data/appdata_*` as `www-data`).

8. **Background jobs and backups.** The `nextcloud-app-cron` CronJob runs
   green, then trigger one fresh backup of every `ReplicationSource`
   (`data-backup*`, `database-backup*`, `data-nfs-backup`) after a manual
   `database-psql-backup` run, so the first post-restore pair exists.

### Recovering from a `secret` mismatch

Only when no matching `config.php` exists any more (app-data repos lost,
or the mismatch was found weeks later). The values encrypted with the old
`secret` are gone; reset them, do not try to "fix" `secret` by hand. Take a
fresh dump first.

| Broken value | Reset |
|---|---|
| `notifications/webpush_vapid_privkey` + `_pubkey` | Delete both rows (`occ config:app:delete notifications webpush_vapid_privkey` and `..._pubkey`); the app generates a new pair (the 2026-11-07 fix). Browsers re-subscribe to push. |
| `contacts/federations_cache` | `occ config:app:delete contacts federations_cache`; it is a cache. |
| Other `$AppConfigEncryption$` rows | Find with the crypto check; delete if the app regenerates them, otherwise re-enter the value in the app's settings. |
| TOTP secrets | `occ twofactorauth:disable <user> totp`, user re-enrolls (backup codes stay valid, they are hashes). |
| App passwords / sessions (`oc_authtoken`) | Unusable: delete in Settings → Security (or `occ user:auth-tokens:delete`) and create new ones; update Doppler (`*_APP_PASSWORD`) and every client that used one (CalDAV/CardDAV, Home Assistant, desktop and mobile sync). |
| files_external credentials, OAuth2 clients | Re-enter the credentials / recreate the client. |

Then rerun the post-restore checklist.

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
