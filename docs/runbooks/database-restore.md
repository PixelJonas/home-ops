# Database restore runbook

Restore a logical PostgreSQL (CNPG) or MariaDB dump into a fresh database and
prove it is complete, consistent and usable before the application starts.

Backups come from `templates/psql-backup/` (CNPG, `pg_dumpall` over the database
Service, structurally validated, atomically renamed to `backup.sql`) and
`templates/mariadb-backup/` (Nextcloud). Each dump PVC is then backed up by a
VolSync ReplicationSource. Related runbooks:

- [volsync-restore.md](volsync-restore.md): restoring the dump PVC itself.
- [storage-rebuild-playbook.md](storage-rebuild-playbook.md): many databases at
  once after a pool rebuild.
- [nextcloud-restore.md](nextcloud-restore.md): the MariaDB/Nextcloud specifics.

Never print SQL, dump contents, row values, role passwords or the Secrets that
hold them. Evidence is sizes, hashes, counts, table/schema lists and log line
classes.

## What "the backup is good" means: four separate layers

A successful backup Job and a healthy restic repository prove that bytes were
stored and can be read back. They do not prove that the dump is complete, that it
is the point in time you need, or that the application can use it. Check each
layer on its own; a pass at one never substitutes for another.

| Layer | Question | Evidence |
|---|---|---|
| 1. Repository integrity | Can restic read the stored objects? | `restic check` (full `--read-data` for high-value local-only repos) |
| 2. Artifact integrity | Is the dump complete and exactly the accepted bytes? | Structural check (below), size and sha256 at capture, after restore, and inside the replay pod |
| 3. Recovery-point suitability | Is it the intended point in time, consistent with the app's other data? | Source counts captured in the same window, recovery-set record |
| 4. Restored-system usability | Does the replayed database plus its files work? | Schema/count comparison, then real application reads in isolation |

A real failure mode from this fleet: a dump streamed through `kubectl exec` stdout
was silently cut off mid-row. The file was non-empty, larger than the previous
one, restic stored it perfectly, and every mover reported success. Only a
structural check (layer 2) showed that it could not be restored.

**Count drift is not truncation.** A structurally valid dump whose counts differ
from today's production is an older point in time, not a broken file. Handle the
two separately. Never edit expected counts to make an old candidate pass.

## Recovery sets

Apps whose state spans a database plus files (documents and media, app config,
object or NFS data, queues) need a **recovery set**: all parts from one recorded
consistency window. Individually valid snapshots taken at unrelated times are not
a recovery set. Record per set:

- set ID, capture time, consistency level (live/crash-consistent or writer-free);
- application and database versions or image digests;
- every snapshot/dump ID and the exact `restoreAsOf` used;
- schema/table list and critical source counts;
- file manifest scope, hashes, numeric ownership and modes;
- caches, queues, exports and external datasets included or deliberately
  excluded (queues and dead-letter state are preserved, never replayed or deleted
  as part of a restore);
- checks deferred to a later step, and why.

## 1. Capture (how a good dump is made)

The shared template already does this; use it as the reference for any new
database route:

1. Connect to the database **Service** with a pinned client whose major version
   is at least the server's. Write with `pg_dumpall --file=<unique tmp>` on the
   dump PVC. Don't stream dump bytes through `kubectl exec` stdout.
2. Require client exit 0, close and fsync before validation.
3. Validate structure before acceptance (next section). A rejected candidate
   never replaces the last good `backup.sql`.
4. Record size and sha256, then rename atomically. The VolSync snapshot runs
   after that.
5. Size the dump PVC for at least 2.5x the dump: the accepted file plus a
   replacement candidate must fit, and alert well before it fills.
6. For a coordinated or final point (migrations), stop the app's writers but keep
   the database up, capture source counts in the same window, then dump.

`--no-role-passwords` means the dump carries roles without passwords. CNPG sets
the application role's password from its own Secret, so this is expected.

### Structural checker requirements (PostgreSQL)

A valid checker must:

- understand quoted identifiers and the real schema/COPY grammar;
- require the expected schema and COPY sets (compare as sets where ordering
  isn't guaranteed);
- require every COPY block's `\.` terminator and every row's declared field count;
- reconcile COPY row counts with the source counts of the same window;
- require a final newline and `-- PostgreSQL database cluster dump complete` after
  the last statement, with only comments or blank lines after it. The per-database
  `-- PostgreSQL database dump complete` footer is not the end: later databases
  follow it;
- validate encoding and delimiters; reject NUL, malformed encoding and bare CR on
  the direct capture path;
- allow legitimate control bytes inside COPY data, recording their position and
  count, never their content;
- be kept honest with synthetic fixtures: truncated, missing footer, wrong field
  count and trailing SQL must all be rejected, and valid dumps accepted.

## 2. Prepare the replay target

1. **Restore the dump PVC** with [volsync-restore.md](volsync-restore.md),
   pinned to the chosen snapshot. Mount it read-only in a helper and record
   `stat` size and `sha256sum` of `backup.sql`. They must match the capture
   record.
2. **Create a fresh, empty target.** For CNPG, an empty data PVC and the Cluster
   from git; all application Deployments, workers, migration Jobs and CronJobs
   stay at 0. For a scratch replay, clone the Cluster spec into a scratch
   namespace (strip `status` and identity metadata, set the scratch
   StorageClass) and materialize its ExternalSecrets there.
3. **Match the source's encoding and locale.** CNPG initializes UTF8 with
   `C` collation by default; a manually created database must use
   `ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0` (or whatever
   the source used). A `SQL_ASCII` target counts bytes instead of characters and
   rejected a valid UTF-8 dump on multibyte rows (a 121-character title is 132
   bytes, too long for `varchar(128)`). Check before replay:

   ```bash
   oc exec -n <ns> "$PRIMARY" -c postgres -- psql -U postgres -d postgres -Atc \
     "SELECT datname, pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname = '<db>';"
   ```

4. **Match versions where you can.** Pin the target image to the server version
   that produced the dump. Server or extension *version* drift is a WARN to record
   and re-evaluate; a different extension *set* fails the restore.
5. **Resolve the primary**; don't assume `<cluster>-1`:

   ```bash
   PRIMARY="$(oc get cluster.postgresql.cnpg.io <cluster> -n <ns> -o jsonpath='{.status.currentPrimary}')"
   ```

6. **Prove it's the new target.** Record the target pod UID and its PV name
   before any `DROP`. `DROP DATABASE` is allowed only on a freshly initialized,
   empty application database, never on a source or a database with data you
   haven't backed up. CNPG bootstrap (and fast app migrations) can pre-create the
   application schema, so terminate connections, drop and recreate it empty:

   ```bash
   oc exec -n <ns> "$PRIMARY" -c postgres -- psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
     -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '<db>' AND pid <> pg_backend_pid();" \
     -c "DROP DATABASE IF EXISTS <db>;" \
     -c "CREATE DATABASE <db> OWNER <owner> ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;"
   ```

### CNPG Cluster "unrecoverable"

A hibernated CNPG Cluster whose PVCs were deleted (for example during a pool
rebuild) is marked `unrecoverable` ("instances previously created but no PVCs")
when hibernation is removed. Recovery: save the Cluster CR to the evidence
directory, confirm the git spec equals the live spec, delete the Cluster and
re-apply it from git. The ESO-managed application Secret is untouched and CNPG
initializes a fresh, empty instance. Do this only for a Cluster in that phase,
and only with authorization naming it.

## 3. Transfer the dump into the replay pod

**Never pipe a dump through `oc exec ... | psql`.** The exec stream can end
early without an error; `psql` then reads a partial last row, reports a missing
column, and the rest of the dump is silently never applied. The same file
replayed over the same path succeeded on a later try, so a single success proves
nothing.

Copy the file into the database pod first, prove the copy, then replay from that
local file:

```bash
# Source side: the read-only dump helper that mounts the restored dump PVC.
oc exec -n <ns> <dump-helper> -- sha256sum /backup/backup.sql

# Copy. This is still an exec stream, so it is checked before anything reads it.
oc exec -n <ns> <dump-helper> -- cat /backup/backup.sql | \
  oc exec -i -n <ns> "$PRIMARY" -c postgres -- sh -c 'cat > /tmp/restore.sql'

# Target side: must equal the source hash and the capture record.
oc exec -n <ns> "$PRIMARY" -c postgres -- sh -c 'stat -c %s /tmp/restore.sql; sha256sum /tmp/restore.sql'
```

A mismatch means copy again; never replay an unverified file. Check `df` first:
the pod-local path needs the dump size free, and copying into the data volume
needs room for the dump plus the restored database (about 3x the dump in total).
When there isn't room, stream from a copy whose hash you verified on the operator
side, hash the stream on the receiving side as it is written, and still check the
replay log for the cluster-dump footer.

## 4. Replay and review the log

```bash
oc exec -n <ns> "$PRIMARY" -c postgres -- sh -c \
  'psql -U postgres -d postgres -f /tmp/restore.sql > /tmp/restore.log 2>&1; echo "rc=$?"'
oc exec -n <ns> "$PRIMARY" -c postgres -- sh -c \
  'grep -c "ERROR" /tmp/restore.log; grep "ERROR" /tmp/restore.log | sed -E "s/^psql:[^ ]*: //; s/\"[^\"]*\"/\"<id>\"/g" | sort | uniq -c'
# Also require the replay to have reached the end of the dump (last COPY and
# statements applied); a truncated input stops early without a final error.
oc exec -n <ns> "$PRIMARY" -c postgres -- rm -f /tmp/restore.sql
```

`pg_dumpall` replays roles and databases that already exist on a CNPG target, so
some errors are expected (`role "postgres" already exists`, the application role,
`database ... already exists` if the dump creates it). Enumerate the expected
diagnostics **before** the replay, as an allowlist of message classes. Every
other line fails the restore: relation, extension, data, permission and
unexpected role/database errors block application startup. A zero exit code with
unreviewed errors is not a pass, and `ON_ERROR_STOP=0` is not a blanket waiver.
Keep the full log in the protected evidence area (it can contain identifiers),
not in tickets or chat.

## 5. Structural comparison

Before the application starts, compare against the source (or the capture
record):

- schema list and table list (as sets);
- exact `count(*)` for every table in a static, quoted list (never `pg_stat`
  estimates; identifiers with schemas, capitals or reserved words must be quoted);
- the extension set (`SELECT extname FROM pg_extension`), with versions recorded;
- server version, encoding and collation.

Then start the application and repeat the counts: migrations that change counts
are expected only where the app version changed, and must be explained.

## 6. Application acceptance

Exercise the real application in isolation (scratch namespace, no Routes, no
external writers, notifications and schedulers off) or, for an in-place restore,
before re-enabling its writers. Per class of app:

- **Git hosting:** repositories in the database match repository directories;
  API visibility, known HEADs, `git fsck`, a scratch clone.
- **Document stores:** search/view/download of a known document; original,
  archive and thumbnail hashes match the restored files. API totals and raw table
  counts can differ legitimately; compare each against its own baseline.
- **File sync (Nextcloud):** `occ status` shows `installed: true` with
  maintenance mode deliberately still on; then existing logins (including second
  factor), shared calendars/contacts, a known file download and a scratch
  upload. See `nextcloud-restore.md`.
- **Password managers:** database counts and a new canary vault are not enough.
  Unlocking an existing vault and downloading an existing attachment needs an
  existing user's credentials, supplied by that user; never put them in evidence.
- **Vector/semantic stores and model gateways:** extensions present *and* used by
  a real query; authenticated model listing against the database-backed
  inventory.
- **Queue-backed services:** record queued, failed and dead-letter state
  separately. An empty main queue doesn't mean the system is drained. Start
  workers only after their dependencies pass, with one controlled canary.

Only after acceptance: start the application normally, take a fresh dump and
backup (the post-restore backup), and re-arm schedules
([volsync-restore.md §4](volsync-restore.md#4-return-to-service)).

## MariaDB (Nextcloud)

Same principles, different tools:

- The root password is only available as a file in the pod
  (`MARIADB_ROOT_PASSWORD_FILE`); read it server-side inside the `exec`, never
  into your shell.
- After `DROP DATABASE`, let the dump's own `CREATE DATABASE` recreate it, so it
  gets the source's character set and collation. Load with
  `--default-character-set=utf8mb4` and confirm the schema's
  `DEFAULT_CHARACTER_SET_NAME` afterwards.
- Copy into the pod and compare sha256 exactly as above, then
  `mariadb ... < /tmp/restore.sql`.

The full procedure is in [nextcloud-restore.md](nextcloud-restore.md).

## Checklist

- [ ] Recovery set and snapshot/dump IDs recorded before starting
- [ ] Dump PVC restored and pinned; size + sha256 match the capture record
- [ ] Target is new and empty; pod UID and PV recorded before `DROP`
- [ ] Encoding/locale match the source; versions recorded
- [ ] Dump copied into the pod; sha256 equal on both ends
- [ ] Replay log reviewed against the pre-declared allowlist
- [ ] Schemas, tables, exact counts and extension set match
- [ ] Application acceptance passed in isolation, writers still off
- [ ] Post-restore backup taken; schedules and sync policies restored
