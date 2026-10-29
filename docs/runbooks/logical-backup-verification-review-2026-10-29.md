# Logical backup and restore verification review

Date: 2026-10-29

Status: evidence-backed review for a future backup-strategy implementation. No changes to the scheduled backup strategy are deployed by this document.

## Purpose

Preserve the lessons from the Altus LVMS migration restore campaign so a future session can improve logical backups and their verification without repeating the same failures. The campaign covered D01-D11, coordinated file/database sets, cache recreation, VM disks, and application-level recovery checks.

The central conclusion is:

> A successful backup Job and a healthy Restic repository prove that bytes were stored and can be read back. They do not prove that a dump is complete, a recovery point is suitable, restored metadata is correct, or an application can use its data.

The raw evidence is archived on the NAS under `/volume1/pmb-backup/altus-lvms-migration/`. These artifacts share the accepted NAS-only risk and are not independently redundant.

## Highest-priority findings

### 1. The shared PostgreSQL backup path can silently truncate dumps

D04 Ingestbuddy supplied a complete failure chain:

- The backup streamed `pg_dumpall` through `kubectl exec` into `backup.sql.tmp`, checked nonzero size, and renamed the file.
- Two examined snapshots ended mid-row in `ingest.bundles`, with 7 fields instead of 21, missing COPY terminators, incomplete table sequences, and no valid final footer.
- The larger truncated snapshot proves that file size and size-class comparisons are not validity checks.
- Restic preserved the truncated bytes exactly. Mover success and repository integrity were therefore true but irrelevant to SQL completeness.
- Evidence supports silent end-of-stream loss in the `kubectl exec` stdout path. The exact client/API/kubelet/CRI hop was not isolated, so a particular upstream bug is not conclusively proven. Source-data and Restic-mover corruption were not supported.

The reviewed worktree still contains this risky pattern in `templates/psql-backup/cronjob.yaml` and the Immich database backup override. Successful D01-D03 imports validate those particular artifacts, not the ongoing backup path.

Key evidence:

- `b05-ingestbuddy-evidence.tar.gz`
- `b05-ingestbuddy-remediation-evidence.tar.gz`
- `b05-ingestbuddy-diagnosis-evidence.tar.gz`, especially `root-cause-assessment.md`
- `b05-ingestbuddy-fresh-v6-evidence.tar.gz`

### 2. Validation must answer four different questions

Keep these acceptance layers separate:

1. **Repository integrity:** can Restic read the stored objects? Use `restic check`, with full `--read-data` for selected local-only recovery candidates.
2. **Artifact integrity:** is the SQL/archive complete and exactly the accepted byte sequence? Check structure, counts, encoding, size, and SHA-256 before acceptance and after restore.
3. **Recovery-point suitability:** does this candidate represent the intended point in time and coordinated set? Compare against the candidate's own source manifest, then document production drift explicitly.
4. **Restored-system usability:** are ownership, modes, labels, application reads, and required writes correct in an isolated consumer?

A pass at one layer never substitutes for another layer.

### 3. Count drift is not truncation, and both need explicit handling

B09 Taxbuddy and B12 Vehicle Pipeline were structurally valid but unsuitable for the requested Gate A baseline: their counts differed. They correctly stopped before import. Fresh writer-free captures reconciled source, dump, and restored counts.

By contrast, D04 was invalid from structure alone; drift cannot explain missing COPY blocks or a final footer.

Routine live backups should validate against counts captured from the same live dump transaction where practical. Final migration backups should use a writer-free window and require exact source/COPY/restore equality. Never edit expected counts merely to make an old candidate pass.

### 4. Coordinated applications need recovery sets, not unrelated artifacts

Nextcloud, Paperless, and Tymeslot demonstrated that application files, databases, media/exports, and external data must come from one recorded consistency window. A independently timed collection of individually valid snapshots is not a coordinated recovery set.

For each recovery set, record:

- recovery-set ID and capture timestamp;
- consistency level: live/crash-consistent or writer-free;
- application and database versions/image digests;
- every snapshot/dump ID and exact `restoreAsOf` timestamp;
- schema/table list and critical source counts;
- file-manifest scope, hashes, numeric ownership, and modes;
- included caches, queues, exports, and external datasets;
- excluded data and deferred application checks;
- production pause/restoration evidence and final health diffs.

### 5. Restore inspection must be non-mutating

B07 Nextcloud initially produced a misleading file mismatch because incompatible mount views were compared and inspector `fsGroup` changed metadata. B08 succeeded with read-only inspection and no inspector `fsGroup`.

File verification should:

- mount source and restored data read-only where possible;
- avoid inspector `fsGroup` unless its mutation is explicitly under test;
- compare compatible relative-path/mount namespaces;
- record path, type, mode, numeric UID/GID, regular-file size, and SHA-256 at a declared full or sampled scope;
- treat root directories separately from nested entries;
- avoid interpreting namespace-specific SELinux MCS categories as portable content;
- report apparent and allocated sizes for sparse images.

## Proposed logical PostgreSQL backup pipeline

This is the recommended future implementation pattern, based on the successful B05 v6 capture and subsequent D-route validations.

1. **Connect directly to the database Service.** Run a pinned, compatible PostgreSQL client in the backup workload. Write with `pg_dumpall -f` directly to a uniquely named temporary file on the dump PVC; do not stream production dump data through `kubectl exec` stdout.
2. **Preflight before pausing writers.** Prove authentication, client version, destination capacity, file creation, close/fsync, atomic rename, and read/hash behavior. Preserve the previous accepted backup throughout capture.
3. **Quiesce only the relevant application when a coordinated/final point is required.** Capture replicas, schedules, backup triggers, and GitOps policies first. Keep the database available. Use an independent watchdog, bounded correct-namespace polling, and idempotent production restoration.
4. **Capture a recovery-point manifest.** Record schema/table inventory and exact critical source counts during the same consistency window.
5. **Require client and filesystem completion.** Dump exit 0, close, and fsync must succeed before validation or rename.
6. **Validate the complete SQL structure before acceptance.** The required checks are listed below. Rejected candidates must never replace the last good dump.
7. **Atomically accept, then snapshot.** Record size and SHA-256, rename only after every gate passes, and create the VolSync/recovery snapshot afterward.
8. **Verify repository and restored bytes.** Pin the snapshot with `restoreAsOf`; run the appropriate Restic check and require restored size/hash equality.
9. **Replay into an isolated database.** Review the entire SQL log with a narrow expected-diagnostic allowlist. Compare the full schema and exact counts before and after application startup.
10. **Exercise the real application.** Perform existing-data reads and scratch-only canaries as described below. Keep services scratch-local and external writers disabled.
11. **Monitor the process, not just the artifact.** Alert on backup age, validation failure, rejected candidates, restore-test age, and missing application checks. Job success alone is not a health signal.

Routine scheduled backups can stop after structural acceptance plus periodic isolated restore drills. Final migration acceptance always requires the isolated replay and application checks.

## PostgreSQL structural checker requirements

Reuse and maintain synthetic positive and negative tests for the checker. A valid checker must:

- support quoted identifiers and the actual schema/COPY grammar;
- require the expected schema and COPY sequences, using set comparison where dependency ordering is not guaranteed;
- require every COPY block's `\.` terminator and every row's declared field count;
- reconcile COPY row counts with the recovery-point source counts;
- require a final newline and `-- PostgreSQL database cluster dump complete` after the last SQL statement, with only comments/blank lines afterward;
- recognize that `-- PostgreSQL database dump complete` is only a per-database footer and valid SQL for later databases may follow it;
- validate encoding and structural delimiters;
- reject NUL, malformed encoding, and bare CR for the proven direct-Linux capture path;
- permit legitimate C0/DEL bytes only inside COPY data, recording byte/count/field locations without content;
- reject truncation, missing cluster footers, field mismatches, and trailing SQL in synthetic negative tests.

The initial B05 checker was wrong in both directions: it used the per-database footer as the final footer and rejected legitimate control bytes. The corrected v6 checker and synthetic calibration are in `b05-ingestbuddy-checker-diagnosis-evidence.tar.gz` and `b05-ingestbuddy-fresh-v6-evidence.tar.gz`.

## Application-specific acceptance catalog

Use this catalog to turn future backup inventory entries into executable restore checks. Counts below describe the tested recovery points, not permanent expected values.

### D01 Gitea

Compare all database tables and exact counts, repository rows with repository directories, API visibility, known HEADs, `git fsck`, and a scratch clone's commit. Runner execution is a separate integration test requiring a deliberately reviewed CI security scope; the B02 restore did not close that runner gap.

### D02 Honcho

Compare all six application counts and schema, read an existing session and peer memory, and test cold model-cache download/load plus nonzero `bge-m3` vectors. Production drift after the pinned point must remain separate from the pinned baseline.

### D03 Immich

Capture database and NFS library state together. Compare all tables/counts, real login, album/photo reads, vector/vchord queries, and an original download whose hash matches the unchanged NFS file.

### D04 Ingestbuddy

Compare all nine required counts, known-bundle content hash through the real store, OCR queue status, and cold empty Valkey. This is the canonical example of a backup that passed Restic checks while being unusable.

### D05 LiteLLM

Compare all tables and five required counts. Verify authenticated `/v1/models` against the database-backed model inventory with `STORE_MODEL_IN_DB=true`. An external model completion needs a separate scratch backend/key and remains a separate integration check.

### D06 Nextcloud

Coordinate MariaDB, application files, NFS data, and empty Redis recreation. Validate full database counts, file/NFS manifests, existing household logins including second factor, shared calendar/contact reads, known-file download, and scratch upload/download. Empty caches do not prove queue or external-data recovery.

### D07 Paperless

Coordinate database, data, media, and export. Validate all tables/counts and complete manifests, then real search/view/download plus original/archive/thumbnail/export hashes. Keep consumers, ingestion, and exporter writes disabled. Raw database and API/ORM document totals can legitimately differ; compare each to its own semantic baseline.

### D08 Taxbuddy

Compare all six counts, existing document/review/transaction/knowledge reads, scratch-only intake and audit canaries, and cold empty Valkey. Record queued/started/scheduled/deferred/failed/DLQ state separately. An empty main queue does not mean the system is drained. The tested production state still has 18 DLQ and 3 failed-job entries requiring explicit review before deletion.

### D09 Tymeslot

Coordinate app files and database, preserve root ownership/modes, compare all tables and required counts, and exercise existing booking, attendee/time, event, availability, and HTTP reads.

### D10 Vaultwarden

Database/config counts and a synthetic vault canary are insufficient. Closure requires a securely supplied existing-user credential or unlocked session to prove existing-vault login/unlock, cipher/collection reads, authenticated existing attachment download, and organization membership. Never record plaintext cipher content or credentials in evidence.

### D11 Vehicle Pipeline

Compare schema/counts and real review/cost/trip reads. Restore detector state without starting detection or sending notifications.

### Additional Jellystat import

Validate the pinned dump's counts and authenticated existing user/library/item plus historical/newest playback reads. The B13 rehearsal does not by itself establish an ongoing backup route.

### V06 qBittorrent

Validate configuration/session state, categories, save paths, state direction, and known completed-data access without a mass recheck. Bind BitTorrent to loopback, disable discovery/external connectivity, stop all session entries, and mount downloads read-only through scratch-only resources. This proves config recovery and access to the external download dataset, not backup of that dataset itself.

### V10-V12 Qdrant

Coordinate data, snapshots, and crawl progress. Compare exact point counts, known-vector result IDs, API/file snapshot consistency, and resume entries without starting a crawl. B15 closed after exact source-derived per-path metadata restoration and compliant SCC admission. Five runtime file changes `0664→0644` and two directory changes `0775→02755` were independently reviewed as application-managed drift; the original contract does not require metadata immutability after the database starts.

### V13 Taxbuddy Hermes

Require exact UID/GID and modes, preserved SSH host keys/pairing/tools home, SQLite `quick_check` for every database, and existing dashboard/chat/kanban reads without starting gateway/herdr/Moshi/tools/agent connections. B16 proved that mover `fsGroup` can cause widespread metadata mutation. Closure passed with mover UID/GID 10000 and `runAsNonRoot` but no `fsGroup`, exact per-path corrections, all 80,425 entries validated, eight SQLite checks, and existing dashboard reads. Source and restore templates now omit `fsGroup`.

### V04/V05 KNX and large VM images

Use at least 1 Gi for a Synology CSI scratch V04 destination even though the source request is 300 Mi. Validate disk identity, NVRAM/vTPM hashes, ownership, modes, SELinux accessibility, and isolated Windows boot before claiming recovery. B17 closure restored all 18 Gate A entries with exact mixed ownership, proved UID/GID 107 read access and destination SELinux labels, and booted Windows 11 to the normal logon screen without BitLocker recovery or external network access. ETS project access remains an interactive Windows Hello PIN check.

Preserve Gate A's mixed ownership exactly; blanket normalization to 107:107 would be a transformation, not preservation. Treat source MCS categories as facts, not scratch destination requirements.

For V03-class sparse images, compare apparent and allocated bytes. Root-safety recovery passed content and boot checks, but allocation grew from about 37 GB to nearly the full 127.86 GB. Capacity planning must tolerate full allocation even when apparent content is unchanged.

## OpenShift SCC and metadata lessons

The SCC authorization root cause is established: helper Pods created directly through `oc-sa` inherit the direct cluster-admin caller's SCC eligibility in addition to their own ServiceAccount's eligibility. The binding `claude-admin-cluster-admin-binding` therefore made `volsync-privileged-mover` available even when a dedicated ServiceAccount had only an `anyuid` RoleBinding. Controller-created Pods consider only their ServiceAccount, so Jobs/Deployments with dedicated ServiceAccounts and pod-template `openshift.io/required-scc` provide the deterministic boundary.

B15 also had a concrete pod-shape constraint: its helper explicitly added `CHOWN`, `FOWNER`, and `DAC_OVERRIDE`, which `anyuid` cannot grant. Exact SCC sorting when multiple SCCs remain eligible is not assumed; the closure removes the unwanted eligibility instead of relying on selection order.

The successful closure contract is:

1. Create helpers through controllers with dedicated ServiceAccounts and `required-scc`; do not create direct helper Pods through the cluster-admin caller.
2. Record the admitted SCC before any exec or write and stop on mismatch.
3. Omit mover and inspector `fsGroup` when exact metadata is required. Expect arbitrary mover ownership/modes and restore the source-derived metadata contract explicitly.
4. Correct only enumerated per-path differences; do not recursively rewrite permissions. Prove pre/post content hashes unchanged and validate every path, type, size, UID/GID, and mode before application startup.
5. Validate as the actual restricted consumer before boot. Record destination SELinux labels rather than copying source MCS categories.
6. Distinguish pre-startup restoration acceptance from later application-managed metadata/content changes.

B15-B17 technical recovery is closed with this pattern. Existing-user application authentication remains a separate acceptance layer.

## Suggested implementation sequence for a future session

1. **Replace the shared PostgreSQL stream first.** Update the common template and Immich override to direct-Service dump files, unique temporary names, fail-closed structural checks, and atomic acceptance. Completion requires synthetic checker tests and one real writer-free capture per changed route.
2. **Add recovery-set manifests.** Cover every D route and coordinated file/DB application, including explicit exclusions and queue/external-data coverage. Completion requires every production PVC to map to a documented recovery or reviewed disposable route.
3. **Create a shared non-mutating verifier.** Support SQL structure, full/sampled file manifests, numeric metadata, sparse allocation, and sanitized evidence. Completion requires negative tests for truncation and metadata mutation.
4. **Schedule isolated restore drills.** Rotate application-level checks from the catalog above and record their age separately from nightly backup success. Completion requires an isolated replay and real application read for every logical database route.
5. **Pre-arrange identity-aware tests.** Supply existing-user credentials or unlocked sessions securely for Vaultwarden and comparable apps without placing secrets in evidence.
6. **Complete identity-aware application acceptance.** B15-B17 technical recovery is closed; use interactive existing-user authentication to close Vaultwarden and KNX/ETS without recording credentials in evidence.
7. **Review queue and sparse-image handling.** Require explicit failed/DLQ replay/deletion decisions and capacity checks based on possible full allocation.

A useful first production hardening candidate is Ingestbuddy because its failure and corrected direct-Service path are fully documented. Do not roll the shared template out broadly until that candidate passes capture, rejection, restore, and application checks with the new implementation.

## Durable evidence index

Authoritative migration results and per-batch checkpoints:

- `docs/runbooks/altus-lvms-migration-restore-tests.md` in this repository.

Key NAS evidence archives:

- `b02-gitea-evidence.tar.gz`
- `b03-honcho-evidence.tar.gz`
- `b04-immich-evidence.tar.gz`
- `b05-ingestbuddy-fresh-v6-evidence.tar.gz`
- `b05-ingestbuddy-checker-diagnosis-evidence.tar.gz`
- `b06-litellm-evidence.tar.gz`
- `b07-nextcloud-evidence.tar.gz`
- `b08-paperless-evidence.tar.gz`
- `b09-taxbuddy-fresh-evidence.tar.gz`
- `b10-tymeslot-evidence.tar.gz`
- `b11-vaultwarden-evidence.tar.gz`
- `b12-vehicle-pipeline-fresh-evidence.tar.gz`
- `b13-jellystat-evidence.tar.gz`
- `b14-qbittorrent-evidence.tar.gz`
- `b15-qdrant-evidence.tar.gz`
- `b16-taxbuddy-hermes-admission-evidence.tar.gz`
- `b16-taxbuddy-hermes-anyuid-evidence.tar.gz`
- `b17-knx-ets-preflight-evidence.tar.gz`
- `b17-knx-ets-1gi-evidence.tar.gz`
- `b17-knx-ets-metadata-evidence.tar.gz`
- `b11-vaultwarden-closure-evidence.tar.gz`
- `b15-qdrant-metadata-closure-evidence.tar.gz`
- `b16-taxbuddy-hermes-closure-evidence.tar.gz`
- `b17-knx-ets-closure-evidence.tar.gz`

Earlier rejected captures and diagnostic archives are retained alongside these files. Use the successful archives for proven acceptance patterns and the failure archives to understand why the controls exist.
