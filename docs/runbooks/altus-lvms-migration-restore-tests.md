# Altus LVMS migration: Gate B restore tests and Knowhere live-migration rehearsal

Prepared read-only on 2026-10-26 against the `feat/altus-knowhere-storage`
branch and OpenShift 4.22.15. This is an execution plan for separately approved
windows. No test, backup, restore, workload scaling, resource creation, or
cleanup was performed while preparing it.

This runbook operationalizes Gate B in
`altus-lvms-migration-backup-restore.md` and the mandatory tests in
`altus-lvms-migration-app-matrix.md`. Those documents remain authoritative for
final quiescence, final backups, production restore of the halted routes, and
Gates A/C. A passed crash-consistent rehearsal does not make a live-writer final
copy acceptable. Knowhere instead uses B18's disposable two-leg live-migration
rehearsal; production Knowhere and its sessions remain running throughout.

## Execution checkpoint: 2026-10-27 network gate

The Xandar `knowhere-lan-bridge-mapping-xandar` NNCP was applied manually from
this branch after a server dry-run and prior-state capture. It reached
`Available=True`, `Degraded=False`, and `1/1 nodes successfully configured`.
The Xandar NodeNetworkState diff contained only the added
`knowhere-lan: br-ex` mapping; the existing `physnet` mapping, `br-ex` uplink,
`br0` storage bridge, and Knowhere's Running/Ready/AgentConnected state remained
unchanged. The policy is not merged into the GitOps source yet.

Two isolated Fedora container-disk guests used the scratch `gb18-knowhere`
namespace and `gb18-knowhere-lan` NAD, with separate MACs, hostnames, firmware
UUIDs, and a disposable SSH key. The Altus guest obtained `10.42.4.75/21` and
the Xandar guest obtained `10.42.4.76/21`. Both passed SSH, DNS, default-route,
and LAN-gateway checks; cross-node ICMP and 1472-byte payload (1500-byte MTU)
checks passed. Neither guest enrolled in Tailscale, so this checkpoint does not
claim Tailscale or migrated-guest identity evidence. Both guests and their
temporary key material were removed; the scratch namespace/NAD remains for the
later B18 rehearsal. No production VM, PVC, or Secret was touched.

## Execution checkpoint: 2026-10-27 initial Gate B batches

- B00 passed all 52 Restic repository checks, including full `--read-data` for every high-value local-only source. InfluxDB snapshot `3af9bd7a` was independently verified after the successful manual backup. Its scheduled replication remains temporarily paused pending the separate mover `spc_t` review.
- The running-production root safety test passed encrypted NAS copy, qcow2 check and SHA-256 verification, scratch iSCSI restore, read-only Ubuntu 24.04.4 filesystem inspection, and an isolated boot with `AgentConnected=True`. The scratch VM, disks, helpers and snapshot were removed; the encrypted artifact and evidence remain on the NAS.
- Top-level B08 passed with approximately 80 MB/s direct writes, 90 MB/s direct reads, 2.8–3.8 ms flush latency, and 26 TiB NAS free space. B09's Knowhere ArgoCD automated sync/prune/selfHeal freeze is active and non-secret metadata was exported.
- Batch B01 Actual Budget passed the pinned `f6d988b1` restore, full file and SHA-256 comparison, UID/GID 1001 read/write checks, real budget/API reads covering 4,575 transactions, and a scratch-only save/re-read. Scratch cleanup and PV claimRef checks passed.
- Batch B02 Gitea passed coordinated V02+D01 restoration: the 2,249-entry repository manifest, all 116 database tables and exact counts, API/directory comparison, nine `git fsck` checks, known HEADs and a scratch clone all matched. Production Gitea, three runners, CNPG, backup schedules and ArgoCD automation were restored and independently verified Healthy/Synced; all 104 production LVMS PVCs remain Bound. The optional representative runner proof remains a GAP because it requires forbidden privileged SCC access. SHA-verified evidence is archived on the NAS as `b02-gitea-evidence.tar.gz`.
- Batch B03 Honcho passed pinned D02 restoration and R63 cold-cache recreation. All six table counts matched Gate A, including 50,652 messages and 19,631 documents; existing session/memory reads succeeded. The empty application volume downloaded and loaded `bge-m3` without permission errors and returned 1,024/1,024 nonzero vector dimensions. Production Honcho was unchanged, scratch cleanup and claimRef checks passed, and SHA-verified evidence is archived on the NAS as `b03-honcho-evidence.tar.gz`.
- Batch B04 Immich passed a fresh coordinated D03 and NFS-library recovery test after a 2m33s Immich-only writer-free window. All 71 tables and exact counts matched, including 7,360 assets and 14,508 asset files; real login, album/photo reads, vector queries and an original download passed, with the downloaded file SHA-matching the unchanged NFS source. Production schedules, replicas and ArgoCD automation were restored and independently verified Healthy/Synced. Cleanup passed and SHA-verified evidence is archived on the NAS as `b04-immich-evidence.tar.gz`.
- Batch B05 Ingestbuddy initially exposed recurring truncation in the shared `kubectl exec pg_dumpall` path: both `1107adaa` and `96acee47` were incomplete despite successful Jobs and Restic checks. The corrected direct-Service writer-free capture passed all structural, encoding, count and final-cluster-footer gates. Fresh snapshot `a83ba034` passed full `restic check --read-data`, exact-hash restore and SQL replay, all nine exact counts before/after startup, known-bundle hash verification, OCR queue reads and cold empty R64 Valkey. An independent watchdog restored production in 291 seconds; final production diffs are empty and both apps Healthy/Synced. SHA-verified success evidence is archived as `b05-ingestbuddy-fresh-v6-evidence.tar.gz`; earlier failure/diagnostic archives are retained. The shared backup template and Immich override now use direct-Service dumps, unique temporary files, fail-closed structural checks and atomic acceptance. Synthetic failure tests, real PostgreSQL 17/18 client-server checks and all ten manifest validations passed; live deployment, writer-free captures and restore drills remain required before final migration.
- Batch B06 LiteLLM passed pinned D05 structural validation and restoration: all 66 tables, exact counts, SQL replay and authenticated `/v1/models` matched the 16-model production baseline with `STORE_MODEL_IN_DB=true`. Production remained unchanged. The explicitly permitted external-completion GAP remains deferred because no separate scratch backend/key was authorized; no production credential was consumed. Cleanup passed and SHA-verified evidence is archived on the NAS as `b06-litellm-evidence.tar.gz`.
- Batch B07 Nextcloud passed coordinated D06+V07+NFS and R58–R61 after recovery from execution timeouts. The initial V07 mismatch was proven to be incompatible mount views plus inspector `fsGroup` metadata mutation, not restored-content corruption: Restic verified all 29,214 files and final snapshot/mount comparisons passed. D06 imported all 137 tables with exact counts; all 932 NFS entries matched; both household logins including TOTP, shared events/contacts, known-file download and scratch upload/download passed; all four Redis volumes were empty with three connected replicas. Production remained Healthy/Synced and its scheduled V07 backup succeeded after removing the stale scratch repository lock. Cleanup passed with 104 Bound production LVMS PVCs; SHA-verified evidence is archived as `b07-nextcloud-evidence.tar.gz`.
- Batch B08 Paperless passed fresh coordinated D07+V08+V09+export recovery after a 265-second Paperless-only writer-free window with watchdog restoration. All 74 tables and exact counts passed; raw `documents_document=1729` and the API/ORM total 1711 matched their respective baselines. V08 (99 entries), V09 (5,114) and export (5,635) matched exactly; all four repositories passed full `restic check --read-data`; three documents passed original/archive/thumbnail/export hashes plus real search/view/download checks. Production schedules and automation were restored, cleanup passed with 104 Bound production LVMS PVCs, and SHA-verified evidence is archived as `b08-paperless-evidence.tar.gz`.
- Batch B09 Taxbuddy initially failed closed because the structurally valid pinned dump had 4,554 transactions versus the 4,575 Gate A baseline. Fresh writer-free direct-Service snapshot `32822301` captured 124 documents, 4,577 transactions, 148 review tasks, 94 knowledge items and 186 audit rows; it passed full Restic read-data, exact-hash/structure/schema/count restoration, real application reads and scratch-only intake/audit canaries. R65 cold-empty Valkey passed. The final 319-second pause restored Healthy/Synced production; 18 DLQ and 3 failed-job entries remain untouched as a separate deletion-review GAP. Cleanup passed and SHA-verified evidence is archived as `b09-taxbuddy-fresh-evidence.tar.gz`; the initial mismatch archive is retained.
- Batch B10 Tymeslot passed coordinated D09+V14 recovery after a 176-second writer-free window. All 40 tables and seven required counts, both full Restic read-data checks, all nine V14 entries plus root ownership/modes/hashes, and real booking/attendee/time/event/availability/HTTP reads passed. Production was restored Healthy/Synced and cleanup passed with 104 Bound production LVMS PVCs. One completed debugging helper was briefly assigned forbidden privileged SCC and immediately deleted; the accepted path added an SCC guard and final inventory contained no privileged pods. A diagnostic DB-URL file was removed before the final zero-finding scan. SHA-verified evidence is archived as `b10-tymeslot-evidence.tar.gz`.
- Batch B11 Vaultwarden is **PASS** after clean technical closure and interactive existing-vault validation. Seven controller-created admissions were compliant; D10 matched all 29 tables/counts and all 772 config files/781 metadata entries matched before/after startup. The user then confirmed existing-vault login/unlock, existing cipher and collection reads, authenticated attachment download and organization membership. The first login hung/showed an unspecified transient error, but a full refresh succeeded; this is recorded as a non-blocking observation without an inferred cause. No credentials or vault content were retained. Scratch access/resources were removed, 104 production LVMS PVCs remain Bound and production Vaultwarden/Knowhere healthy. Interactive evidence is SHA-archived as `b11-vaultwarden-interactive-evidence.tar.gz`, with technical-closure and initial-failure archives retained.
- Batch B12 Vehicle Pipeline initially failed closed because the valid pinned dump had 95 ingest events versus Gate A's 96. Fresh writer-free direct-Service snapshot `db4862d1` captured 96 events and passed all 19-table schema/count, structural/encoding/footer, full Restic read-data and pinned-restore checks. SQL replay and real review/cost/trip API reads passed with restored detector state but no detector startup/notifications. Production recovered in 129 seconds with writers/schedules/automation restored; cleanup passed with 104 Bound production LVMS PVCs and no privileged SCC. SHA-verified success evidence is archived as `b12-vehicle-pipeline-fresh-evidence.tar.gz`; the initial mismatch archive is retained.
- Batch B13 Jellystat passed the additional pinned import rehearsal using snapshot `47295e5e` after rejecting the drifted older Gate A candidate before import. All 14 tables matched, including 8 users, 6 libraries, 856 library items, 686 playback activities and 7,420 logging rows; authenticated existing user/library/item and historical/newest playback reads passed. Production remained unchanged with no pause or new backup, scratch ran under `restricted-v2`, cleanup passed with 104 Bound production LVMS PVCs, and SHA-verified evidence is archived as `b13-jellystat-evidence.tar.gz`.
- Batch B14 qBittorrent passed pinned V06 recovery: all 469 files and Gate A ownership/modes matched, UID/GID 1029:65537 read/write/delete checks passed, and WebUI/API validated 279 session entries, 8 categories, exact save paths/state direction and 19 known completed-data files without mass recheck. Four later production additions were correctly excluded from the pinned baseline. BitTorrent was loopback-only with no Gluetun/Route/external HTTPS/DHT/PeX/LSD/UPnP or production writes; downloads stayed read-only. Production was unchanged, cleanup passed with 104 Bound production LVMS PVCs, and SHA-verified evidence is archived as `b14-qbittorrent-evidence.tar.gz`.
- Batch B15 Qdrant is **PASS after reviewed closure**. Controller-created helpers with dedicated ServiceAccounts and `required-scc` eliminated the direct cluster-admin caller's SCC union; all 23 captured admissions were compliant. Exact source-derived per-path corrections restored every path's content and metadata before startup, including roots `0:1001090000:2777`, with no recursive writes or content changes. Exact collections/counts, known-vector results, five snapshots and 7,744 resume entries passed without a crawl. Independent acceptance review confirmed that five runtime file changes `0664→0644` and two directory changes `0775→02755` are non-blocking application-managed drift; the original contract does not require post-startup metadata immutability. Closure evidence is SHA-archived as `b15-qdrant-metadata-closure-evidence.tar.gz`, with the initial failure and uncorrected-stop archives retained.
- Batch B16 Taxbuddy Hermes is **PASS after reviewed closure**. The mover used UID/GID 10000 and `runAsNonRoot` without fsGroup; all 14 helper admissions were controller-created and limited to approved `anyuid`/`nonroot-v2`. Exact per-path corrections restored 80,087 nested modes without recursive writes or content changes; all 80,425 entries and 3.16 GB then matched Gate A, including root `10000:10000:0700` and required config/session/SSH/Moshi/tools state. Read-only `quick_check` passed for all eight SQLite databases and dashboard-only existing chat/Kanban reads passed with no external services or metadata drift. Cleanup passed with 104 Bound production LVMS PVCs and unchanged production Hermes/Knowhere. SHA-verified evidence is archived as `b16-taxbuddy-hermes-closure-evidence.tar.gz`; earlier failures remain archived. The V13 source/restore templates now omit fsGroup.
- Batch B17 KNX is **PASS** under the revised stopped-VM backup/isolated-restore contract. Fresh coordinated V04 snapshot `8e7bb8ab` and V05 snapshot `3a65d8ef` were captured on 2026-10-29 while production remained Stopped/Halted with no VMI or source attachments. Both full Restic read-data checks and exact Direct restores passed. Exact Gate A mixed per-path metadata, unchanged NVRAM/vTPM hashes, UID/GID 107 read-only access and destination SELinux labels passed; the reviewed current `disk.img` size is 64063799296 bytes. Non-boot exact restore validation was sufficient under the user's acceptance decision; the earlier isolated Windows boot also passed without BitLocker recovery. All nine fresh helper admissions were controller-created with no forbidden SCC. Scratch cleanup passed, production KNX remained stopped, and Knowhere stayed Running/Ready/AgentConnected. Evidence and per-file hashes are archived in `b17-knx-ets-cold-backup-20261029/` with manifest SHA-256 `b3dbc1a06d36a2eac76bf625395ffa6963f6da55b2b03e85b7202c2f981979b2`; earlier technical evidence is retained as `b17-knx-ets-closure-evidence.tar.gz`.
- Batch B19 is **PASS after reviewed correction**. The initial RHEL restored-blob assumption was invalid because those DataImportCrons use direct upstream `pullMethod: node` and never cached the blobs internally. O02 retained-artifact restoration passed all 122 entries, 44 full hashes/sparse maps, metadata, ACL/xattrs and normalized SELinux. `_catalog`, unchanged Qdrant manifest `48566bd1`, and all 19 linked config/layer blobs passed real SHA-verified pulls with upstream fallback network-disabled; scratch-only push/pull also passed. I04 RHEL 10.2, I05 RHEL 9.8 and I06 RHEL 8.10 passed independent upstream DataVolume imports and isolated AgentConnected boots, without being counted as O02 blob evidence. Cleanup passed, production registry/pruner/source remained unchanged, KNX stopped and Knowhere healthy. Original artifact SHA-256 `2ed0b4050a6096e67070b6f0d63e9e8b2b7ba7aac2b8244e960274a066628789`; closure archive `b19-image-registry-evidence-closure.tar.gz` SHA-256 `3161db184b5fb04681fa260f75fb12542897ef0af3b30a2cefc4a663fcdd882f`; initial failure/diagnosis archives remain retained.
- Batch B20 platform monitoring is **PASS**. O03/O04 encrypted archive/extract checks passed full hashes, numeric ownership/modes, ACL/xattrs, SELinux and exact logical sparse maps. Alertmanager preserved all four silences and notification-log hash with no notifications/alert groups. Prometheus retained all 25 TSDB blocks, checkpoint and every WAL segment; historical `kube_node_info` and `node_load1` matched exactly at four timestamps including oldest retained history. Scraping/rules/remote write/admin APIs/external networking were disabled. Cleanup removed all scratch and custom SCC/RBAC resources; production monitoring specs/config/silences/CVO/source identities remained unchanged and Knowhere healthy. Evidence archive `b20-monitoring-evidence.tar.gz` SHA-256 `184381a0206a4c4d10569744e4b62a1d615e66ff3ce3eef27155c1dddf24589c`.
- Batch B21 UWM monitoring is **PASS**. O05 and O06 snapshot archive/extract checks covered all 200 and 180 entries respectively, including full hashes, sparse extents, ownership/modes, ACL/xattrs and SELinux. Production-image scratch workloads ran as 65534:65534 under `nonroot-v2` with no privileged SCC. O05 exactly matched 10,212 user-metric points over seven days and 95 `VolSyncBackupStale` points over 48 hours after WAL replay, without deletion/trimming. O06 restored local state and evaluated all four rules in two groups; the known snapshot-time query returned 99 series with no scraping, remote write, Alertmanager or notification egress. Cleanup and production spec/retention/CVO reconciliation passed with Knowhere healthy. Evidence archive `b21-uwm-evidence.tar.gz` SHA-256 `e718c62d1ded868e1ed354b3b654aa91c91228304ba088a77e6c6de52e56ca0c`.
- Batch B22 Paperless MCP has **PASSING technical recovery with one application GAP**. O07 encrypted archive/extract, full content/numeric metadata/sparse/ACL/xattr/SELinux and arbitrary UID/fsGroup checks passed. Production-image scratch workloads preserved one DCR client, 11 access and 11 refresh sessions by logical bbolt SHA-256; signing-key/HMAC hashes and JWKS matched. Health/OAuth metadata returned 200 and unauthenticated `/mcp` 401 under `restricted-v2`, with no privileged SCC. Existing-client interactive login, existing authorized-session use and authenticated Paperless search remain open because no interactive Authelia credential or existing OAuth bearer token was available; authentication was not bypassed. Cleanup and production/ArgoCD reconciliation passed, with Knowhere healthy across 41 samples. Artifact `O07-gb22-20261029T163828Z.tar.gpg` SHA-256 `dd3b977101c7b87aa8157c40fd747ecb988307be715778920f9460267c155036`; evidence archive SHA-256 `f7f5cc857d2c45bee6373bcf3469fcff0ad883e61b61da28ed10b7f02229c61f`.
- Batch B23 pre-recreation cache checks are **PASS**. R62 Docling started with an empty 5Gi PVC under arbitrary UID/`restricted-v2`, passed `/health`, downloaded 16 new Hugging Face cache files and converted a known PDF to expected text and one nonempty 15-token chunk. The isolated VolSync REST probe rejected unauthenticated access, completed manual snapshot `000ebc7c`, and created a new Bound 1Gi cache on `synology-iscsi-storage`. No production repository/snapshot or post-recreation `lvms-knowhere` probe was used. Cleanup and production Docling diff passed, with Knowhere healthy. Evidence archive `b23-empty-caches-evidence.tar.gz` SHA-256 `f4901a661da04fbafaa54172bf4e39a8a4eb7cb55d77ea615a78e15039bda50c`.
- Batch B24 managed-image recreation is **PASS**. I01 CentOS Stream 10, I02 CentOS Stream 9 and I03 Fedora each imported to a raw 30Gi Block/RWO `synology-iscsi-storage` PVC with DataVolume Succeeded and DataSource-equivalent readiness. Isolated CentOS Stream 10 and Fedora 44 Cloud boots reached AgentConnected with zero network interfaces; I02 shares the CentOS-family boot proof. Manifest/amd64-child digest differences were representation-only with no unexpected source-version drift. All scratch resources were removed, production managed-image objects had empty before/after diffs, no privileged SCC was used, and Knowhere stayed healthy. Evidence archive `b24-managed-images-evidence.tar.gz` SHA-256 `e19d8bb46c6ac2b98ac2c604b05a6b61ab39b452315a68aaac67352a1a7caccf`; evidence manifest SHA-256 `de4f0586b4cf837253fa1c99f6ae25e5dc5eb3adecfe03617cbaf40e90853f5c`.
- B18a first attempt **aborted before migration** under the fail-closed health contract. Altus iowait reached 21.3917% twice, exceeding the 20% threshold. V03 snapshot `9305bed9` restored Successfully in 678 seconds, but its scoped anyuid helper lacked `CHOWN` and could not set disposable `/disk.img` to 107:107. No migration plan, probes, interim iSCSI copies, backup, B18b, pool rebuild or source deletion occurred. Disposable VM remains Stopped/Halted with its two vg1 sources and restore cache Bound. Production Knowhere stayed healthy across 222 samples, KNX stayed stopped, and all production before/after captures matched. Abort evidence `b18a-knowhere-evidence-abort.tar.gz` SHA-256 `582d23af42d70b9cbd1ba49bba5bbc8a37b84272356744bf4ed92b89ea3c72cd`; retry requires fresh health preflight and a corrected helper.
- B18a retry passed the CHOWN-only controller Job and 901-second quiet preflight (Altus iowait max 0.9917%, root max 73.0948%, etcd WAL p99 max 18.52ms), then **FAILED at compute migration**. Storage plan `85110d4a-da3d-4f8a-9863-47fb46c04e46` and StorageMigration `85a23413-13ff-4e2e-94ae-7b25fed6179a` created both bound iSCSI destinations. The projects format/stage timed out on Altus during slow ext4 formatting and concurrent CSI retries, then recovered automatically; Xandar had no staging error. The deterministic compute blocker is `host-model` CPU scheduling: Xandar lacks `fb-clear`, `ibrs-all`, `mds-no`, `rdctl-no` and `tsx-ctrl`, while Altus is excluded by source/target anti-affinity. Controller retries cannot succeed unaided. Disposable VMI remains Running/AgentConnected on Altus with vg1 sources active/retained; destinations are not active and must not be booted because later writes remain on sources. Network/app max pauses were 1.019s/1.007s and both writers contiguous with no boot/mount/data changes. Fresh scratch backup was not run. No cancellation/reversal/B18b/pool/source deletion occurred; production Knowhere and KNX captures matched. Failure archive `b18a-knowhere-retry-evidence-failure.tar.gz` SHA-256 `426aa55f7eadfc092e15e17d54c8af29b89b4d6b90c3285d80b80f3d6f6495d3`.
- B18–B24, B09's production queue-deletion review, B06's deferred external completion, live shared D-route backup rollout/capture validation, and B02's optional runner proof remain open unless separately recorded below. The B18a maximum pause is approved at 30 seconds per leg with a target of at most 10 seconds and applies only to disposable Knowhere rehearsals, not KNX. Production Knowhere stayed Running throughout all completed checks.

## Safety and completion contract

- Execute from outside Knowhere. Independent batches may run in parallel when
  explicitly authorized, provided they have disjoint source/scratch resources,
  combined capacity/load headroom, independent evidence, and continuous Knowhere
  health checks; otherwise run one at a time. On 2026-10-29 the user explicitly
  authorized the stopped KNX backup/restore to run alongside B19. B18 is
  deliberately split: run B18a after B17, then B19-B24 and all other pre-rebuild
  gates; retain the running B18 guest on interim iSCSI through the rebuild and
  run B18b before the production return leg. B18's two disks are one plan, not
  independent batches. Clean each batch after sign-off except the explicitly
  retained B18 sources/active interim set.
- Gate A must already be complete. Use the approved off-node export
  `10.10.10.40:/volume1/pmb-backup/altus-lvms-migration`, its encryption design,
  pinned helper images, `restic` 0.18.0, read-only guest tools, and minimum-SCC
  helpers recorded in the Gate A runbook.
- `synology-iscsi-storage` is the default scratch class below. B18a starts its
  disposable two-disk VM on `lvms-vg1` and migrates to iSCSI before the rebuild.
  B18b runs only after vg1 is rebuilt and `lvms-knowhere` exists, and migrates
  the active iSCSI disks back to that class. The new class's filesystem/Block
  provisioning and R10 cache selection are therefore post-recreation acceptance
  checks, not circular pre-cutover prerequisites. V03 remains a DR-only restore
  on `lvms-vg1` scratch; neither production Knowhere disk is restored.
- Requested capacities are reservations, not estimates of real data. Before
  each batch, compare source allocated bytes plus the batch's expected artifact
  growth with real free space, and retain at least 25% free after the batch.
  Requests do not justify thin-pool overcommit. Stop for Altus root-disk use
  above 80%, host iowait above 20%, abnormal etcd WAL latency, or an unexpected
  source change.
- Never print or copy Secret objects. Re-materialize existing ExternalSecrets
  in the scratch namespace and check only `SecretSynced`. Do not put SQL dumps,
  restic output containing credentials, kubeconfigs, vault item contents, or
  application tokens in evidence.
- Use `anyuid` only where the existing workload or numeric-owner helper needs
  a fixed UID/root. Never use `privileged`, host PID/network, hostPath, or
  `oc debug node` for these tests. Copy only existing namespaced RBAC; remove
  temporary RBAC before namespace deletion.
- Do not copy Routes, production MACs, USB host devices, production VM network
  attachments, or production writer identities. Scratch apps use namespace-local
  Services or port-forward only. Block unsolicited external writes and do not
  resume downloads, crawls, ingestion, CI, notifications, or agent sessions.
- Evidence precedes cleanup. A failed check fails the batch even if its pod is
  Ready or its mover says Successful. Preserve the failed namespace for
  diagnosis only with an explicit operator decision; otherwise record failure
  evidence and clean it using the same allowlist.
- Deleting a scratch namespace must never delete a source snapshot, repository,
  production PVC, or production DataVolume. Verify the namespace and all names
  against the batch row immediately before deletion.

## Exact batch order and capacity bounds

The scratch request column includes dump-staging copies and both sides of the
offline archive tests. It excludes VolSync mover caches unless listed. NFS
artifact space is based on allocated source data, not these requests.

| Batch | Scratch namespace | Coverage | Maximum scratch requests |
|---|---|---|---:|
| B00 | per-source `gb00-check-NNN` Pods only | repository identity/checks, baselines | 0 PVC; bounded emptyDir cache |
| B01 | `gb01-actual-budget` | V01 | 5Gi |
| B02 | `gb02-gitea` | V02+D01; representative R49/R52/R53 runner | 300Gi, including 5Gi dump and one 265Gi runner set |
| B03 | `gb03-honcho` | D02; R63 cold model cache | 20Gi |
| B04 | `gb04-immich` | D03 and NFS-library consistency | 25Gi |
| B05 | `gb05-ingestbuddy` | D04; R64 empty Valkey | 26Gi |
| B06 | `gb06-litellm` | D05 | 13Gi |
| B07 | `gb07-nextcloud` | V07+D06+NFS user data; R58-R61 empty Redis | 143Gi |
| B08 | `gb08-paperless` | V08+V09+D07 plus export consistency | 63Gi |
| B09 | `gb09-taxbuddy` | D08; R65 empty Valkey | 16Gi |
| B10 | `gb10-tymeslot` | V14+D09 | 12Gi |
| B11 | `gb11-vaultwarden` | V15+D10 | 8Gi |
| B12 | `gb12-vehicle-pipeline` | D11 | 10Gi |
| B13 | `gb13-jellystat` | additional Jellystat import rehearsal; not a D route | 10Gi |
| B14 | `gb14-qbittorrent` | V06 | 5Gi |
| B15 | `gb15-qdrant` | V10+V11+V12 | 16Gi |
| B16 | `gb16-taxbuddy-hermes` | V13 | 5Gi |
| B17 | `gb17-knx-ets` | current V04+V05 and isolated Windows boot | 64Gi+300Mi; reserve 65Gi |
| B18a | `gb18-knowhere` | disposable two-disk live migration leg 1, altus → xandar; V03 DR-only initial projects restore | 854.38Gi concurrent across `lvms-vg1` + iSCSI |
| B18b | `gb18-knowhere` | the same running disposable VM and both active disks, return leg xandar → altus | 854.38Gi concurrent across iSCSI + `lvms-knowhere` |
| B19 | `gb19-image-registry` | O02; I04/I05/I06 one at a time | 330Gi: two 150Gi registry PVCs plus one 30Gi image |
| B20 | `gb20-monitoring` | O03+O04 | 100Gi |
| B21 | `gb21-user-monitoring` | O05+O06 | 160Gi |
| B22 | `gb22-paperless-mcp` | O07 | 2Gi |
| B23 | `gb23-empty-caches` | R62 and representative R01-R09/R11-R48 VolSync cache; R10 is post-recreation only | 10Gi maximum |
| B24 | `gb24-managed-images` | I01/I02/I03 one at a time | 30Gi maximum |

B02's runner proof is representative of all three runner sets. B23's generic
cache proof is representative of R01-R09 and R11-R48; R10 is checked separately
on `lvms-knowhere`. R50/R51 and R54-R57 use the same controller and storage
pattern as the tested runner. Final Gate C still reconciles all 65 R and all six
I names; Gate B does not convert them into backup routes.

Each Knowhere disk set reserves 427.19Gi (300Gi root plus 136579960013 bytes
projects). `retentionPolicy: keepSource` makes source and destination coexist, so each leg needs
854.38Gi across its two classes, plus mover-cache headroom and the 25% margin.
B18a's reviewed vg1 scratch-source cleanup precedes the rebuild; its interim
iSCSI copy remains active for B18b. Production source retention and teardown are
separate gates and are not implied by scratch cleanup.

## Common evidence and preflight pattern

Set an off-node, access-controlled evidence directory supplied by Gate A. Keep
one subdirectory per batch. Every `result.tsv` row must contain:

```text
batch  route  startedUTC  finishedUTC  operator  source_namespace
source_pvc  source_snapshot_or_dump  artifact_sha256  artifact_bytes
scratch_namespace  scratch_pvc  target_pv  target_class  target_request
restore_exit  validation_exit  cleanup_exit  result  evidence_file
```

Use `PASS` only when every field applicable to the route is present and all
commands passed. `FAIL`, `ABORT`, and `NOT_RUN` are not interchangeable. Record
restore duration and the exact command/log filenames. Logs must be reviewed for
secrets before leaving the protected evidence directory.

Capture the live inventory and abort on drift from the two source runbooks:

```bash
oc-sa altus get pvc -A -o json | jq -r '
  .items[] | select(.spec.storageClassName == "lvms-vg1")
  | [.metadata.namespace,.metadata.name,.spec.volumeMode,
     (.spec.accessModes|join(",")),.spec.resources.requests.storage,
     .spec.volumeName] | @tsv' | sort > "$EVIDENCE/inventory-before.tsv"
test "$(oc-sa altus get pvc -A -o json | jq '[.items[] | select(.spec.storageClassName == "lvms-vg1")] | length')" = 104
oc-sa altus version -o yaml > "$EVIDENCE/openshift-version.yaml"
oc-sa altus get storageclass,volumesnapshotclass -o yaml > "$EVIDENCE/storage-classes.yaml"
```

Also capture workload specs (without Secrets), numeric ownership/modes,
file/count/size manifests, checksums, source snapshot IDs/timestamps, and the
named exact row counts before testing. For each checksum manifest state whether
it is full or sampled; all small config/state volumes and all O artifacts use
full checksums. Do not hash large application trees repeatedly during the final
outage.

### Scratch construction

For each `gbNN-*` namespace:

1. Create the exact namespace in the batch table. Label it
   `lvms-migration-test=gate-b`; do not add it to an ApplicationSet.
2. Copy only required non-secret ConfigMaps, Services, ServiceAccounts,
   Roles/RoleBindings and workload specs from production. Strip `status`,
   `resourceVersion`, `uid`, `creationTimestamp`, owner references, Routes and
   production ingress. Replace the namespace with the scratch namespace. Keep
   production resource names inside that namespace unless a batch says
   otherwise. Pin images to the captured production digest.
3. Copy ExternalSecret *specifications*, never their generated Secrets. The
   following safe transform preserves Doppler key names and references while
   replacing object identity:

   ```bash
   SRC=paperless
   NS=gb08-paperless
   oc-sa altus -n "$SRC" get externalsecrets.external-secrets.io -o json |
     jq --arg ns "$NS" '.items[] |
       {apiVersion,kind,metadata:{name:.metadata.name,namespace:$ns},spec:.spec}' |
     oc-sa altus apply -f -
   oc-sa altus -n "$NS" wait --for=condition=Ready externalsecrets --all --timeout=5m
   ```

4. Reproduce only existing SCC grants with namespaced RoleBindings in the
   scratch namespace. Convert no ClusterRoleBinding wholesale. A fixed-UID app
   that already requires `anyuid` gets a scratch RoleBinding to its own
   ServiceAccount; a numeric-owner archive helper gets a separate
   `gbNN-archive-helper` ServiceAccount and `anyuid` RoleBinding. Restricted-v2
   remains the default.
5. Apply a default-deny ingress NetworkPolicy and namespace-local-only egress,
   adding only explicitly required DNS, image pulls, object storage, NAS, or
   test dependency egress. Do not expose a Route. Before app startup, prove no
   production database, queue, storage PVC, or VM identity is referenced.
6. Use the production PVC names for cloned workloads in their scratch
   namespace. ReplicationDestinations and helper objects use the exact `gbNN`
   names below. A scratch dump PVC is named `database-syno-db-backup`, request
   5Gi, Filesystem/RWO, on the scratch class.

Create each V/dump destination with this shape, substituting only the table's
exact name, namespace and request. Do not add a data source to an empty
VolSync destination:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: paperless-ng-data
  namespace: gb08-paperless
spec:
  accessModes: [ReadWriteOnce]
  volumeMode: Filesystem
  storageClassName: synology-iscsi-storage
  resources:
    requests:
      storage: 3Gi
```

Clone a CNPG resource only after its ExternalSecrets are synchronized. This
safe transform strips live identity/status and forces the reviewed scratch
class; create no application Deployment or migration Job with it:

```bash
SRC=gitea
NS=gb02-gitea
CLUSTER=gitea-db
CLASS=synology-iscsi-storage
oc-sa altus -n "$SRC" get cluster.postgresql.cnpg.io "$CLUSTER" -o json |
  jq --arg ns "$NS" --arg class "$CLASS" '
    del(.status) | .metadata |= {name:.name,namespace:$ns} |
    .spec.storage.storageClass = $class' |
  oc-sa altus apply -f -
oc-sa altus -n "$NS" wait --for=condition=Ready "cluster.postgresql.cnpg.io/$CLUSTER" --timeout=15m
oc-sa altus -n "$NS" get cluster.postgresql.cnpg.io "$CLUSTER" \
  -o jsonpath='{.status.currentPrimary}{"\n"}'
```

Use the analogous metadata transform for the Immich database Deployment and
Nextcloud MariaDB StatefulSet. For MariaDB also set every
`volumeClaimTemplates[].spec.storageClassName` to the scratch class; do not
clone its running Pod or copy a Secret. Start only that database resource.
Clone application Deployments/StatefulSets later, initially with zero replicas,
and increase them only after all storage checks pass.

### VolSync restore pattern

Create a Bound, empty destination first, then this shape with the exact values
in the V table. Use `copyMethod: Direct`; no destination writer may exist.

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: restore-v08
  namespace: gb08-paperless
spec:
  trigger:
    manual: gb08-v08-YYYYMMDDTHHMMSSZ
  restic:
    repository: restic-config-data-b2
    destinationPVC: paperless-ng-data
    copyMethod: Direct
```

Record the current operation's completion time and require
`.status.latestMoverStatus.result == "Successful"`; a Synchronizing condition
alone is insufficient. V13 additionally requires
`moverSecurityContext: {runAsUser: 10000, runAsGroup: 10000,
runAsNonRoot: true, fsGroup: 10000}`. The Nextcloud NFS restore requires uid/gid
33 and `runAsNonRoot: true`. KNX and Knowhere ownership corrections are limited
to their documented trees/files, not arbitrary recursive application chowns.

| Route(s) | Batch | Scratch destination PVC(s), request | Repository Secret / ReplicationDestination |
|---|---|---|---|
| V01 | B01 | `actual-budget-app-data`, 5Gi | `restic-config-data-b2` / `restore-v01` |
| V02 | B02 | `gitea-shared-storage`, 20Gi | `restic-config-data` / `restore-v02` |
| V03 | B18a | `knowhere-projects`, 136579960013 bytes, Filesystem on `lvms-vg1`; disaster-recovery evidence only, never a production live-path restore | `restic-config-projects` / `restore-v03` |
| V04 | B17 | `persistent-state-for-windows-11-virtio-ets`, 300Mi | `restic-config-persistent-state` / `restore-v04` |
| V05 | B17 | `windows-11-virtio-ets-volume`, 64Gi | `restic-config-volume` / `restore-v05` |
| V06 | B14 | `qbittorrent-config`, 5Gi | `restic-config-qbittorrent-b2` / `restore-v06` |
| V07 | B07 | `nextcloud-app-nextcloud`, 20Gi | `restic-config-data-b2` / `restore-v07` |
| V08/V09 | B08 | `paperless-ng-data`, 3Gi; `paperless-ng-media`, 15Gi | `restic-config-data-b2` / `restore-v08`; `restic-config-media-b2` / `restore-v09` |
| V10/V11/V12 | B15 | `cosmere-knowledge-index-progress`, 1Gi; `qdrant-app-data`, 10Gi; `qdrant-app-snapshots`, 5Gi | `restic-config-crawl-progress` / `restore-v10`; `restic-config-data` / `restore-v11`; `restic-config-snapshots` / `restore-v12` |
| V13 | B16 | `taxbuddy-hermes-app`, 5Gi | `restic-config-data` / `restore-v13` |
| V14 | B10 | `tymeslot-app`, 5Gi | `restic-config-data-b2` / `restore-v14` |
| V15 | B11 | `vaultwarden-app-config`, 1Gi | `restic-config-config` / `restore-v15` |
| non-LVMS | B04 | `immich-app-library`, 2000Gi only if a library restore is separately required; normally validate against unchanged NFS | `restic-config-library`; no Gate B restore required |
| non-LVMS | B07 | `nextcloud-app-nextcloud-data`, 100Gi on `synology-nfs-storage` | `restic-config-data-nfs` / `restore-v07-nfs` |
| non-LVMS | B08 | `paperless-ng-export`, 20Gi on `synology-nfs-storage` | `restic-config-export-b2` / `restore-v08-export` |

For every filesystem restore compare relative paths, file counts, sizes,
checksums at the declared scope, numeric owners/modes, and readability under
the intended arbitrary UID/fsGroup. Record destination PV name and SELinux
accessibility; do not compare MCS categories across different namespaces.

### Database import pattern

Each D batch restores its dump into the scratch 5Gi staging PVC with the listed
repository and `restore-dNN-dump` ReplicationDestination. Require nonzero
`/backup/backup.sql`, record size and SHA-256 without printing it, then start
only the cloned database. The clone uses the exact current production image,
storage request, database and owner below. CNPG resource and data PVC names stay
unchanged inside the scratch namespace; resolve `.status.currentPrimary` rather
than assuming `-1`.

Before import, record target pod UID and new PV, prove the target namespace is
correct, terminate app connections, and drop/recreate only the empty application
DB. Replay the full dump with `psql -U postgres -d postgres` for
PostgreSQL/CNPG. Keep and review the entire error log. Expected duplicate global
role diagnostics must be enumerated in the evidence; any relation, extension,
data, permission, or unexpected role/database error fails the batch. Immich uses
its Deployment/container `immich-app-database`/`database`; Nextcloud follows the
server-side `MARIADB_ROOT_PASSWORD_FILE` procedure in `nextcloud-restore.md`.
No application migration Job or writer starts before import validation.

| Route | Batch | Source/scratch DB resource and data PVC | DB/owner | Repository / RD | Exact application counts (all required) |
|---|---|---|---|---|---|
| D01 | B02 | CNPG `gitea-db`; `gitea-db-1`, 10Gi | `gitea`/`gitea` | `restic-config-database` / `restore-d01-dump` | `repository`, `user`, `action_runner`, `action_run` |
| D02 | B03 | CNPG `honcho-db`; `honcho-db-1`, 5Gi | `honcho`/`honcho` | `restic-config-database` / `restore-d02-dump` | `workspaces`, `peers`, `sessions`, `messages`, `documents`, `collections` |
| D03 | B04 | Deployment `immich-app-database`; `immich-app`, 20Gi | `immich` | local+B2 checks; restore `restic-config-database-b2` / `restore-d03-dump` | `user`, `asset`, `album`, `asset_file`, `library`, `kysely_migrations` |
| D04 | B05 | CNPG `ingestbuddy-db`; `ingestbuddy-db-1`, 20Gi | `ingestbuddy`/`ingestbuddy` | `restic-config-database` / `restore-d04-dump` | `ingest.bundles`, `ingest.ingest_events`, `ingest.sink_dispatches`, `ingest.known_identifiers`, `ingest.ocr_event_state`, `ingest.ocr_funnel_counters`, `ingest.ocr_queue_control`, `ingest.ocr_urgent`, `ingest.ocr_waiting` |
| D05 | B06 | CNPG `litellm-db`; `litellm-db-1`, 8Gi | `litellm`/`litellm` | `restic-config-database` / `restore-d05-dump` | `LiteLLM_ModelTable`, `LiteLLM_ProxyModelTable`, `LiteLLM_Config`, `LiteLLM_VerificationToken`, `LiteLLM_TeamTable` |
| D06 | B07 | StatefulSet `nextcloud-app-mariadb`; `data-nextcloud-app-mariadb-0`, 10Gi | `nextcloud` | restore `restic-config-database-b2` / `restore-d06-dump` | `oc_users`, `oc_filecache`, `oc_calendars`, `oc_calendarobjects`, `oc_addressbooks`, `oc_cards` |
| D07 | B08 | CNPG `paperless-db`; `paperless-db-1`, 20Gi | `paperless`/`paperless` | restore `restic-config-database-b2` / `restore-d07-dump` | `documents_document`, `documents_note`, `documents_tag`, `documents_savedview`, `paperless_mail_processedmail` |
| D08 | B09 | CNPG `taxbuddy-db`; `taxbuddy-db-1`, 10Gi | `taxbuddy`/`taxbuddy` | `restic-config-database` / `restore-d08-dump` | `tax.documents`, `tax.transactions`, `tax.review_tasks`, `tax.knowledge_items`, `tax.audit_log`, `tax.alembic_version` |
| D09 | B10 | CNPG `tymeslot-db`; `tymeslot-db-1`, 2Gi | `tymeslot`/`tymeslot` | restore `restic-config-database-b2` / `restore-d09-dump` | `users`, `meeting_types`, `meetings`, `meeting_guests`, `availability_schedules`, `calendar_integrations`, `schema_migrations` |
| D10 | B11 | CNPG `vaultwarden-db`; `vaultwarden-db-1`, 2Gi | `vaultwarden`/`vaultwarden` | restore `restic-config-database-b2` / `restore-d10-dump` | `users`, `ciphers`, `folders`, `attachments`, `collections`, `devices` |
| D11 | B12 | CNPG `vehicle-pipeline-db`; `vehicle-pipeline-db-1`, 5Gi | `vehicle_pipeline`/`vehicle_pipeline` | `restic-config-database` / `restore-d11-dump` | `vehicle_pipeline.vehicles`, `vehicle_pipeline.review_items`, `vehicle_pipeline.costs`, `vehicle_pipeline.ingest_events`, `trips.trips`, `trips.odometer_readings`, `trips.notify_state` |
| extra | B13 | CNPG `jellystat-db`; scratch `jellystat-db-1`, 5Gi | `jellystat`/`jellystat` | `restic-config-database` / `restore-jellystat-dump` | `jf_users`, `jf_libraries`, `jf_library_items`, `jf_playback_activity`, `jf_logging` |

At preparation time the CNPG images were PostgreSQL 18.4-system-trixie except
Ingestbuddy at 18.6-system-trixie; Immich was
`ghcr.io/immich-app/postgres:17-vectorchord0.3.0-pgvectors0.3.0`; MariaDB was
`docker.io/bitnamilegacy/mariadb:11.4.6-debian-12-r0`. Recapture and pin digests
at execution. Drift requires refreshing the baseline, not silently changing the
test. Immich must prove both `vectorchord` and `vector` extensions and run real
queries using them, not merely list installed extensions.

Generate static SQL from the table list above. Run `count(*)`, never
`pg_stat` estimates, and diff source/target count files and schema/table lists.
Identifiers containing schema, capitals, or reserved words must be quoted from
the static list. Capture no row values except non-secret canary identifiers
explicitly approved for evidence.

### Cleanup pattern

After evidence capture, scale/delete throwaway VMs first and wait for VMIs and
VolumeAttachments to disappear. Delete temporary RoleBindings/ServiceAccounts,
ReplicationDestinations, VolumeSnapshots and explicitly listed helper objects.
Then verify the namespace label and delete only that namespace:

```bash
NS=gb08-paperless
test "$(oc-sa altus get namespace "$NS" -o jsonpath='{.metadata.labels.lvms-migration-test}')" = gate-b
oc-sa altus -n "$NS" get pvc -o name > "$EVIDENCE/$NS/cleanup-pvcs.txt"
oc-sa altus get vm,vmi -n "$NS" > "$EVIDENCE/$NS/cleanup-vms.txt"
oc-sa altus delete namespace "$NS" --wait=true --timeout=20m
oc-sa altus get pv -o json | jq -r '.items[] | select(.spec.claimRef.namespace == "'"$NS"'") | .metadata.name' > "$EVIDENCE/$NS/pvs-after-cleanup.txt"
```

The last file must be empty. Record cleanup exit status. Never prune restic or
remove the off-node evidence/artifacts as part of scratch cleanup.

## B00: repository integrity and executable baseline

Enumerate the exact ReplicationSources for every V/D row plus Jellystat,
Nextcloud NFS, Immich library, Paperless export, and the seven media config
sources. Include both local and B2 legs where listed. Compare the set by name
with the routing ledger before creating checks.

For each source sequentially, create `gb00-check-NNN` in its source namespace
with `envFrom.secretRef` on the listed repository Secret, an emptyDir
`RESTIC_CACHE_DIR`, and the installed restic image. Record latest snapshot
ID/time, nonzero dump/artifact evidence, and `restic check` scope. Use
`--read-data-subset=5%` initially; use full `--read-data` for the local-only
Gitea data, Knowhere projects, both KNX, all three Qdrant, Taxbuddy Hermes,
Vaultwarden config, Nextcloud NFS and Immich library repositories before Gate
C. Do not run all full reads concurrently.

Pass: every required repository has a current snapshot and clean check; every
baseline manifest/count file exists; real NAS/B2/scratch free capacity plus 25%
margin is recorded. Fail: stale/missing snapshot, read-data error, unreviewed
source drift, or insufficient allocated capacity. No PVC cleanup is required;
delete only the numbered check Pods after their evidence is captured.

## B01-B16: VolSync and coordinated application batches

Each row below is executed with the common restore/import pattern and the exact
names in the V/D tables. Application Services stay scratch-local. Read checks
are mandatory; canary writes are allowed only into the scratch system.

- **B01 Actual Budget:** restore V01 and start the cloned app as
  1001:1001/fsGroup 1001. Compare the full small-volume checksum/owner manifest,
  budget list, an existing budget's balances and recent transactions, then save
  and re-read one scratch-only validation change. Fail on root ownership,
  read-only data, or a count/checksum mismatch.
- **B02 Gitea:** restore V02+D01 from one recorded recovery set. Start CNPG,
  import, then Gitea. Compare repository DB rows with repository directories;
  run `git fsck`, verify known HEADs, clone one scratch repository and verify
  its commit. Recreate only runner ordinal 0 empty with
  `data-runner-gitea-actions-app-runner-0` 5Gi,
  `gitea-actions-app-runner-0-dind-storage` 250Gi and
  `gitea-actions-app-runner-0-runner-cache` 10Gi. Register it to scratch Gitea,
  run a real canary CI job, and record overlay2, per-job MTU 1400, successful
  build, and no Altus root-disk growth. Old DinD/cache content must be absent.
- **B03 Honcho:** import D02 and recreate `honcho-app` empty (10Gi). Start the
  model runtime with a cold cache, prove the model downloads/loads without
  permission errors, then compare all six table counts, read an existing
  session/memory, call `bge-m3`, and record nonzero vectors of the baseline
  dimension. Fail if the old model cache appears or `/health` is not clean.
- **B04 Immich:** import D03 into the exact custom image, prove required
  extensions and migrations, then start only the scratch server dependencies.
  Compare all counts, open an existing album/photo, download its original, and
  compare its checksum with the corresponding unchanged NFS library file. Do
  not restore or write the production library during the normal test.
- **B05 Ingestbuddy:** import D04 into schema `ingest`, recreate
  `ingestbuddy-app` empty (1Gi), and verify Valkey starts with zero keys. Before
  final R64 deletion, separately record queue-empty evidence from the app's OCR
  queue status and a bounded Valkey scan; do not infer drained work from pod
  readiness. Compare all named counts and open a known bundle. Any controlled
  end-to-end canary must use scratch Paperless/Qdrant/LiteLLM/Docling and a
  scratch inbox, never production dependencies.
- **B06 LiteLLM:** import D05. Compare all five tables and authenticated
  `/v1/models` against baseline. Confirm `STORE_MODEL_IN_DB=true` behavior by
  changing no Git model list and observing the restored models. A low-cost
  completion must use a scratch key/scratch spend record and must not expose or
  consume a production key; otherwise defer only that external write to the
  production post-restore checks, not the DB import acceptance.
- **B07 Nextcloud:** restore V07, D06 and `nextcloud-app-nextcloud-data` from
  one continuous maintenance-window recovery set. Use uid/gid 33 for the NFS
  mover and preserve app config ownership. Recreate
  `redis-data-nextcloud-app-redis-master-0` and all three replica PVCs empty
  (2Gi each). Start MariaDB alone, import, then Redis and app. Require restored
  `config/config.php`, `php occ status` installed=true, correct version,
  maintenance initially on and deliberately disabled only for validation.
  Compare all six counts; log in as both household users; verify shared
  calendars/events, contacts, known file checksum/download, and one scratch
  upload/download. Restore maintenance state to its captured pre-test value
  before cleanup only if the scratch app is left running during evidence review.
- **B08 Paperless:** restore V08+V09+D07 and the non-LVMS export from the same
  writer-free set. Start CNPG/import then Paperless with consumers disabled.
  Compare all five table counts and API total; for selected documents verify
  original/archive/thumbnail/export paths and checksums, then search, view and
  download a known document. Do not enable ingestion or exporter writes.
- **B09 Taxbuddy:** import D08 into schema `tax` and recreate `taxbuddy-app`
  empty (1Gi). Prove empty Valkey startup and record production queue-drain
  evidence before final R65 deletion. Compare all six counts, open a known
  document/review item and run a known lookup. A canary intake and audit
  writeback must use only scratch dependencies.
- **B10 Tymeslot:** restore V14+D09 from the same stopped-app set. Compare all
  seven table counts; open an existing booking, verify attendee/time and
  availability, and prove `/app/data` files are readable by the root-running
  scratch app without broad permission changes.
- **B11 Vaultwarden:** restore V15+D10 from the same writer-free set. Use the
  scratch namespace's arbitrary UID/fsGroup. Compare all six table counts, log
  into an existing vault, read only a known non-secret item name, verify an
  attachment checksum and organization membership. Never print vault secrets.
- **B12 Vehicle Pipeline:** import D11, preserving schemas
  `vehicle_pipeline` and `trips`. Compare all seven counts, open a known
  review/cost and trip, and verify restored detector state without enabling
  new notifications.
- **B13 Jellystat:** import the additional current dump even though its
  production DB remains on Synology and no D route is assigned. Compare all
  five counts, open an existing user/library, and verify historical and newest
  playback rows. Do not recreate or import into the production `jellystat-db-2`.
- **B14 qBittorrent:** restore V06 and start only a scratch qBittorrent
  container/WebUI with Gluetun, downloads and external egress disabled.
  Preserve numeric ownership and prove app uid 1029/gid/fsGroup 65537 can read
  and write. Compare torrent count, categories, save paths and states; verify
  known data is found without a mass recheck. Port-forward the WebUI. VPN
  forwarded-port and resumed-download checks belong to production startup.
- **B15 Qdrant:** restore V10+V11+V12 from one writer-free set and start the
  unprivileged image with fsGroup 1001090000. Compare collection list and exact
  `points_count`, including `cosmere_knowledge` and `ingestbuddy_documents_v1`;
  compare snapshots API with restored files and validate
  `coppermind_progress.json`/resume position. Execute a known vector query and
  compare result IDs. Do not start a crawl; registry-backed crawl validation is
  separate.
- **B16 Taxbuddy Hermes:** restore V13 with the 10000 mover context. Require
  0700 data root and 0600 Moshi socket state, uid/gid 10000:10000, preserved SSH
  host keys/pairing and tools home. Run SQLite `quick_check` on each restored
  DB and compare config/session manifests. Start only dashboard/read paths with
  gateway, herdr, Moshi and agent execution disabled; open an existing chat and
  kanban item. A real agent turn is a post-restore check, not permission for the
  scratch clone to contact production LiteLLM/Honcho/Taxbuddy.

## B17: current KNX coordinated restore and boot

Use `docs/runbooks/knx-ets-volsync-restore-test.md`, with this runbook's exact
namespace `gb17-knx-ets` substituted throughout and with a new current backup
snapshot pair. Do not accept the 2026-10-11 result as current evidence. The
2026-10-29 acceptance decision requires only a stopped-VM backup and isolated
restore; interactive ETS authentication and KNX live migration are excluded.

Before taking the fresh backup, verify that production
`windows-11-virtio-ets` is stopped, its VMI and disk attachments are absent,
and it remains stopped. A guest shutdown request or `runStrategy=Always` is not
proof of this precondition. Do not stop production implicitly when preflight
finds it Running. Back up V04/V05 as a coordinated pair only while both source
disks are unmounted, then leave production unchanged.

Restore V04/V05 to the exact PVC names above in the scratch namespace. Set the
state PVC label `persistent-state-for: windows-11-virtio-ets-test`; clone the VM
as `windows-11-virtio-ets-test`, initially Halted. Remove `devices.hostDevices`,
use MAC `02:e7:4f:bd:a6:38`, and retain firmware serial and UUID
`b73ae2a8-c599-45f0-9830-33e50088fae3` byte-identical. Use controller-created
helpers with dedicated ServiceAccounts and `openshift.io/required-scc`; record
admitted SCC before exec or writes. No privileged SCC or production dongle is
permitted. The 2026-10-27 read-only V04/V05 baseline also required the exact
mixed source MCS set `s0:c0,c35,c190,c439`: `/src` used `c0,c35`, while nested
state used `c190,c439`. Inspect restored labels and use the minimal matching
category set; never relabel production data.

Before boot, restore exact source-derived per-path ownership and modes without
recursive rewrites or content changes, and require:

```text
/root/disk.img                                    expected current size 64050167808 bytes, or reviewed drift
/state/nvram/windows-11-virtio-ets_VARS.fd         exists and checksum recorded
/state/swtpm/b73ae2a8-c599-45f0-9830-33e50088fae3/tpm2/tpm2-00.permall
```

Require every restored path to match the captured mixed-ownership baseline;
blanket normalization to 107:107 fails. Record both source snapshot IDs
together; a pair from unrelated recovery points fails. Boot only the throwaway
VM and require `AgentConnected=True`, Windows 11 guest OS and no BitLocker
recovery. No interactive ETS project or USB-license check is required. Halt the
scratch VM, wait for its VMI and attachments to disappear, capture evidence,
then clean up only scratch resources.

## B18: disposable Knowhere two-leg live-migration rehearsal

B18 uses only a disposable VM and disposable network/guest identities. It does
not read, restore, attach, stop, or reboot either production Knowhere disk or
VM, and it does not require production agent/herdr sessions to stop. Production
Knowhere remains running for the entire migration process. O01 sparse/qcow2
root evidence may remain only as separately approved backup-safety evidence;
it is outside this live-migration mechanism and is not a B18 pass criterion.

A separate mandatory safety gate covers the production root without stopping it:
take a running-VM VolumeSnapshot, explicitly labeled crash-consistent unless the
guest agent confirms a successful filesystem freeze, and restore that snapshot
to a new scratch Block PVC. Never attach the production source to a helper. Run
qcow2-LUKS backup/check/SHA-256 verification, read-only guest inspection, and
the reviewed isolated-boot recovery check against only the scratch copy. Record the
snapshot/recovery point and any application-consistency limitation. Keep this
off-node recovery artifact through the observation period. This safety evidence
is required before production leg 1, but it is not a continuous migration copy
and must never be used to replace the running production root.

Root-safety templates are `altus-lvms-root-safety-support.yaml`,
`altus-lvms-root-safety-backup.yaml`,
`altus-lvms-root-recovery-restore.yaml`,
`altus-lvms-root-recovery-inspect.yaml`, and
`altus-lvms-root-recovery-vm.yaml` in this directory. Use them only in the
approved Gate B window with a unique artifact ID and reviewed scratch identity.
The recovery VM has `autoattachPodInterface: false`, empty interfaces/networks,
and no production PVC, MAC, IP, cloud-init Secret, or firmware UUID. Use
`.status.guestOSInfo` from the VMI rather than requiring `virtctl`.

### Preflight and identity

1. Record the verified installed `VirtualMachineStorageMigrationPlan` API and
   storage-migration controller, plus KubeVirt LiveUpdate and BlockMigration
   support. Direct or same-node migration is impossible for this design; do not
   substitute one. Each leg pairs one two-disk storage plan with the specified
   running-VM compute migration.
2. Configure and validate the `knowhere-lan` OVN localnet mapping on xandar
   before B18a. Prove equivalent layer-2 attachment on altus and xandar without
   using production Knowhere's MAC, IP, cloud-init Secret, Tailscale identity,
   or external Route. Freeze Knowhere GitOps around production volume-reference
   changes; the disposable VM must not join an ApplicationSet.
3. Create RWO PVCs `knowhere-rootdisk` (300Gi, Block) and `knowhere-projects`
   (136579960013 bytes, Filesystem) on `lvms-vg1` in `gb18-knowhere`. Seed the
   root from a reviewed non-production test image. Restore V03 to this scratch
   PVC as disaster-recovery evidence: require
   `/disk.img`, its apparent/allocated size, scoped project manifests, and
   `chown 107:107 /disk.img` only. This V03 restore is never a production step.
   Use a controller-created Job with a dedicated custom SCC granting only
   `CHOWN`, no fsGroup and `openshift.io/required-scc`; verify that exact SCC
   through the downward API before the write. Do not use a direct helper Pod,
   `anyuid`, privileged SCC, `FOWNER`, `DAC_OVERRIDE`, or recursive rewrites.
4. Create and initially boot disposable VM `knowhere-live-migration-test` on
   altus with both disks, disk serial `projectsdisk`, and its own disposable
   firmware UUID, MAC/IP, cloud-init, and guest hostname. Disable production
   agent/Tailscale enrollment. Record this identity and the guest boot ID. After
   this initial boot, no shutdown or reboot is permitted during either leg.
5. Before any B18a retry, complete a quiet 15-minute preflight with samples at
   least every 20 seconds: every Altus iowait sample must remain ≤20%, root use
   <80%, and no restore/migration/mover workload may be active. Record per-device
   utilization/throughput and etcd WAL p99 on both nodes; investigate sustained
   90–100% device saturation or abnormal WAL elevation. Reconfirm Knowhere's
   unchanged identity and health immediately before booting the disposable VM.

Executable templates are `docs/runbooks/altus-lvms-b18-initial.yaml`,
`docs/runbooks/altus-lvms-b18-leg1.yaml`, and
`docs/runbooks/altus-lvms-b18-leg2.yaml`. They are deliberately separate apply
files; never apply all three together. The retained scratch NAD is referenced,
not replaced. B18a's approved maximum acceptable migration pause is **30 seconds per leg**,
with an operational target of 10 seconds or less. The one-second continuous
probes must measure each leg independently against this limit; any longer pause
fails the rehearsal and stops further legs or pool changes.

Start independent one-second network and guest-application probes before leg 1
and leave them running through leg 2. A guest writer must append and fsync a
monotonic sequence to files on both root and projects disks. Record the maximum
pause against the approved brief-migration-pause budget; require no unexplained
gap, duplicate, or corruption. Guest boot ID and uptime must remain continuous,
and firmware UUID, hostname, MAC/IP, disk serials, mounts, and project manifests
must remain unchanged. A plan reporting success without these checks is a fail.

### B18a: lvms-vg1 to interim iSCSI, altus to xandar

1. Create one `VirtualMachineStorageMigrationPlan` naming the disposable VM and
   **both** active PVCs, targeting `synology-iscsi-storage`, with `retentionPolicy: keepSource`
   enabled and no automatic source deletion. Migrate the running VM from altus
   to xandar. Do not split root and projects into separate plans and do not halt
   the VM to make storage migration work.
2. Require successful completion for both disks, active root/projects PVs on
   `synology-iscsi-storage`, the running VMI on xandar, retained vg1 sources,
   unchanged identity/boot evidence, and passing availability/data probes.
3. Repoint the scratch `projects-backup` ReplicationSource to the active iSCSI
   projects PVC, set `volumeSnapshotClassName: synology-snapshot`, and set its
   `cacheStorageClassName` to `synology-iscsi-storage` (never `lvms-vg1`). Trigger a
   new backup and require `latestMoverStatus.result=Successful`, a current
   snapshot, and repository evidence from the active destination.
4. A failed plan, migration, availability check, or backup stops the process:
   do not rebuild vg1. Cancelling a migration after it has completed is not
   rollback. Preserve source/destination evidence and require a separately
   reviewed recovery action rather than automatic reversal.

The B18a resources have this exact shape; the apply file is
`docs/runbooks/altus-lvms-b18-leg1.yaml`. Substitute only the disposable
namespace and reviewed destination names after checking for collisions:

```yaml
apiVersion: migrations.kubevirt.io/v1alpha1
kind: VirtualMachineStorageMigrationPlan
metadata:
  name: gb18-leg1
  namespace: gb18-knowhere
spec:
  retentionPolicy: keepSource
  virtualMachines:
    - name: knowhere-live-migration-test
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
---
apiVersion: migrations.kubevirt.io/v1alpha1
kind: VirtualMachineStorageMigration
metadata:
  name: gb18-leg1
  namespace: gb18-knowhere
spec:
  virtualMachineStorageMigrationPlanRef:
    name: gb18-leg1
```

After B18a passes and all independent rebuild gates are satisfied, review and
record deletion of only the disposable vg1 source objects required for the
rebuild; `retentionPolicy: keepSource` must not trigger automatic deletion. Rebuild vg1 and
create `lvms-knowhere`, then validate Block and Filesystem provisioning and the
final snapshot class. This is the boundary between B18a and B18b, not a circular
requirement to create `lvms-knowhere` before the old pool can be removed.

### B18b: active iSCSI to lvms-knowhere, xandar to altus

The apply file is `docs/runbooks/altus-lvms-b18-leg2.yaml`; use it only after
B18a, the rebuild, and final storage-class/provisioning checks pass.

1. With the same disposable VM still running on xandar, create one plan for
   **both then-active iSCSI PVCs**, targeting `lvms-knowhere`, with `retentionPolicy: keepSource`
   and no automatic deletion. Migrate the running VM from xandar to altus. Do
   not use the retained vg1 objects as migration sources.
2. Require both active PVs on `lvms-knowhere`, the running VMI on altus,
   unchanged identity/boot evidence, and continuous availability/data checks.
   Keep the interim iSCSI source PVCs for the defined observation period.
3. Repoint scratch `projects-backup` to the active `lvms-knowhere` projects PVC,
   set `volumeSnapshotClassName: lvms-knowhere` and
   `cacheStorageClassName: lvms-knowhere`, then require a second fresh
   successful backup from that active destination. Record the new cache PV and remove no backup repository.
4. Preserve plan/migration IDs, before/after node and PVC/PV/class bindings,
   source-retention evidence, probe timelines, boot IDs, writer sequences,
   snapshot IDs, backup results, and operator sign-off for both legs. Cleanup of
   disposable retained sources occurs only after evidence review. Production
   interim iSCSI sources remain for observation, and production old vg1 source
   deletion requires its own review before teardown.

The production legs repeat these same storage, node, GitOps-freeze, identity,
availability, and per-leg VolSync gates without a production restore, shutdown,
reboot, or session stop. Any failed leg stops the rebuild or next leg. Neither
post-completion cancellation nor source deletion is a rollback mechanism.

## B19-B22: six offline filesystem archive types

For Gate B, avoid a production outage by using one VolumeSnapshot per source,
then a temporary source-restore PVC in the **source namespace**. Snapshot and
source-restore names are exactly `gbNN-oNN-source-snap` and
`gbNN-oNN-source`. Use class `lvms-vg1`, VolumeSnapshotClass `lvms-vg1`, the
original request/mode/access mode, and wait for `readyToUse=true` before the
read-only helper starts. The production writer is not scaled for this rehearsal.
The final migration copy still requires the operator-safe stop/detach sequence
below. This is the exact two-resource pattern (shown for O04; substitute only
the table's batch/route, namespace, PVC and request):

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: gb20-o04-source-snap
  namespace: openshift-monitoring
spec:
  volumeSnapshotClassName: lvms-vg1
  source:
    persistentVolumeClaimName: prometheus-k8s-db-prometheus-k8s-0
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gb20-o04-source
  namespace: openshift-monitoring
spec:
  accessModes: [ReadWriteOnce]
  volumeMode: Filesystem
  storageClassName: lvms-vg1
  resources:
    requests:
      storage: 40Gi
  dataSource:
    apiGroup: snapshot.storage.k8s.io
    kind: VolumeSnapshot
    name: gb20-o04-source-snap
```

Wait for the named snapshot's `.status.readyToUse=true`, record its
`.status.creationTime` and bound snapshot content, then create the restored PVC
and its read-only helper. `lvms-vg1` uses `WaitForFirstConsumer`, so waiting for
the restored PVC to bind before creating its consumer deadlocks; instead require
the helper/PVC to become Ready/Bound together. Mount `gb20-o04-source` read-only
in the archive helper. The scratch extraction target is a separate empty PVC on
`synology-iscsi-storage`; do not use the snapshot-restored source as the
extraction destination.

For metadata helpers, record process and source SELinux labels before traversal.
The V06 restored root required `s0:c45,c1004`; O03–O05 were `unlabeled_t:s0` and
required read-only `spc_t`. These are narrow helper contexts, not privileged SCC
grants or permission to relabel production data. Scratch application validation
must still use the target workload's own security context.

Archive each source to a fresh off-node artifact
`ONN-gbNN-YYYYMMDDTHHMMSSZ.tar.gpg` using the Gate A streaming tar → GnuPG
AES-256 pattern with `--one-file-system --numeric-owner --sparse --xattrs
--acls --selinux`. Require nonzero ciphertext size, decrypt-and-tar-list with
`pipefail`, ciphertext SHA-256 and apparent/allocated size; never create a
plaintext tar on NFS. Decrypt and extract into the empty target PVC listed
below in the scratch namespace using `--same-owner` and the same preservation
flags. Capture before/after full path/type/mode/numeric-owner/size manifests,
file counts, sparse block counts, ACL/xattr checks and full checksums. Validate
SELinux readability under the target workload security context; do not demand
the source namespace's MCS label in scratch.

| Batch/type | Source namespace/PVC, request | Scratch target PVC | Real application evidence |
|---|---|---|---|
| B19/O02 registry | `openshift-image-registry` / `image-registry`, 150Gi | `image-registry`, 150Gi | Full repository/blob/upload manifest; `_catalog`; fully cached `qdrant/cosmere-knowledge-index` manifest digest and all linked blobs unchanged; real pull with upstream disabled; disposable scratch push/pull; no GC/dedup |
| B20/O03 Alertmanager | `openshift-monitoring` / `alertmanager-main-db-alertmanager-main-0`, 10Gi | same name, 10Gi | Silence IDs/expiry and notification state match; known silence loads; no notifications sent |
| B20/O04 platform Prometheus | `openshift-monitoring` / `prometheus-k8s-db-prometheus-k8s-0`, 40Gi | same name, 40Gi | TSDB blocks/WAL and min/max times; known metric at oldest retained and several pre-cutover timestamps; exact samples match |
| B21/O05 UWM Prometheus | `openshift-user-workload-monitoring` / `prometheus-user-workload-db-prometheus-user-workload-0`, 40Gi | same name, 40Gi | Oldest/newest known user metric, >26h `VolSyncBackupStale` range, no 7d trimming or WAL deletion |
| B21/O06 Thanos Ruler | `openshift-user-workload-monitoring` / `thanos-ruler-user-workload-data-thanos-ruler-user-workload-0`, 40Gi | same name, 40Gi | Rule groups, local state/data and evaluation timestamps; known rule/query works without notifications |
| B22/O07 Paperless MCP | `paperless-mcp` / `paperless-mcp-app`, 1Gi | `paperless-mcp-app`, 1Gi | OAuth DCR clients/signing keys/sessions unchanged; existing-client login; authorized session where feasible; authenticated read-only Paperless document search |

Run each application against its restored scratch target with the captured
production image. For registry, use a minimal scratch registry configuration
with storage at `/registry`, no production TLS Secret or pull-secret volume,
and no public Route. Annotate ClusterIP Service `image-registry` with
`service.beta.openshift.io/serving-cert-secret-name: image-registry-tls` and
mount that service-generated certificate at `/etc/secrets`; configure registry
HTTP TLS with `/etc/secrets/tls.crt` and `/etc/secrets/tls.key`. Copy the public
CA from the scratch namespace's `openshift-service-ca.crt` ConfigMap into
ConfigMap `image-registry-ca`, key `ca.crt` (do not read the TLS Secret), and
set `certConfigMap: image-registry-ca` on every registry-backed DataVolume.
Use `pullMethod: pod`; do not change node or cluster insecure-registry settings.
For Prometheus, disable scraping and remote write; for
Alertmanager/Thanos, disable notification egress. Starting a scratch service
must not prune WAL, compact away history, regenerate keys or run registry GC.
Use port-forward and non-secret HTTP evidence. Record numeric ownership
65534:65534 for monitoring and the scratch arbitrary UID/fsGroup for registry
and Paperless MCP (production Paperless MCP was 1001020000 at inventory; recheck
rather than treating it as universal).

After evidence, delete the scratch app/namespace, then the exact source-restore
PVC, helper RBAC and VolumeSnapshot in the source namespace. Confirm the source
PVC and production workload remained unchanged throughout.

## Operator-safe final-copy quiescence and exact restoration

These steps are for the final offline copies, not needed merely to read a
VolumeSnapshot in B19-B22. Record all files in the protected evidence area
before changing anything. The expected 4.22.15 starting state is supplied only
as a drift check; restore captured values, not these examples.

### Registry O02

Capture management, replica and pruner state without Secrets:

```bash
oc-sa altus get configs.imageregistry.operator.openshift.io/cluster -o json |
  jq '{spec:{managementState:.spec.managementState,replicas:.spec.replicas}}' \
  > "$EVIDENCE/registry-config-before.json"
oc-sa altus get imagepruners.imageregistry.operator.openshift.io/cluster -o json |
  jq '{spec:{suspend:.spec.suspend,schedule:.spec.schedule}}' \
  > "$EVIDENCE/registry-pruner-before.json"
oc-sa altus -n openshift-image-registry get deployment image-registry -o json |
  jq '{spec:{replicas:.spec.replicas}}' > "$EVIDENCE/registry-deploy-before.json"
oc-sa altus -n openshift-image-registry get deployment/cluster-image-registry-operator \
  deployment/image-registry cronjob/image-pruner -o wide > "$EVIDENCE/registry-workloads-before.txt"
```

Expected now: Config `Managed`, replicas 1, Deployment 1, pruner `suspend=false`
and schedule `0 0 * * *`. Pause builds/RHEL imports first. Set the **ImagePruner**
`.spec.suspend=true` (this object has no `managementState` field), wait for any
active pruner Job to finish naturally, and prove no new pruner Job starts. Then
patch Config `.spec.managementState=Unmanaged`, scale `deployment/image-registry`
to zero, and require no registry/pruner pod and no source VolumeAttachment. Do
not scale the CVO-owned registry operator Deployment:

```bash
oc-sa altus patch imagepruners.imageregistry.operator.openshift.io/cluster \
  --type=merge -p '{"spec":{"suspend":true}}'
until test "$(oc-sa altus -n openshift-image-registry get cronjob/image-pruner -o jsonpath='{.spec.suspend}')" = true; do sleep 5; done
oc-sa altus -n openshift-image-registry get cronjob/image-pruner job pod -o wide
# Continue only after the operand CronJob is suspended, every active pruner Job
# has completed, and no writer remains.
oc-sa altus patch configs.imageregistry.operator.openshift.io/cluster \
  --type=merge -p '{"spec":{"managementState":"Unmanaged"}}'
oc-sa altus -n openshift-image-registry scale deployment/image-registry --replicas=0
oc-sa altus -n openshift-image-registry wait --for=delete pod \
  -l docker-registry=default --timeout=10m
SOURCE_PV="$(oc-sa altus -n openshift-image-registry get pvc/image-registry -o jsonpath='{.spec.volumeName}')"
test -n "$SOURCE_PV"
oc-sa altus get volumeattachments.storage.k8s.io -o json |
  jq -r --arg pv "$SOURCE_PV" '.items[] | select(.spec.source.persistentVolumeName == $pv)'
# There must be no attachment for the current source PV.
```

After the final archive and validation, reverse in this order:

1. With Config still Unmanaged, restore the captured registry Deployment
   replica count and wait Ready.
2. Restore Config `.spec.managementState` and `.spec.replicas` from
   `registry-config-before.json`; require the operator to reconcile Ready.
3. Restore ImagePruner `.spec.suspend` and `.spec.schedule` from
   `registry-pruner-before.json`; do not force an immediate prune.
4. Query `_catalog`, pull a known digest and perform the approved disposable
   push/pull. Compare the final Config, pruner and Deployment state to the three
   before files. No management state or schedule may be left at the maintenance
   value.

The three patch files already contain only the intended `spec` fields:

```bash
oc-sa altus -n openshift-image-registry patch deployment/image-registry \
  --type=merge --patch-file="$EVIDENCE/registry-deploy-before.json"
oc-sa altus -n openshift-image-registry rollout status deployment/image-registry --timeout=10m
oc-sa altus patch configs.imageregistry.operator.openshift.io/cluster \
  --type=merge --patch-file="$EVIDENCE/registry-config-before.json"
oc-sa altus patch imagepruners.imageregistry.operator.openshift.io/cluster \
  --type=merge --patch-file="$EVIDENCE/registry-pruner-before.json"
oc-sa altus -n openshift-image-registry get deployment/image-registry \
  -o jsonpath='{.spec.replicas}{"\t"}{.status.readyReplicas}{"\n"}'
```

### Platform and user-workload monitoring O03-O06

OpenShift 4.22's installed CMO configuration reference exposes no `replicas`
field for `prometheusK8s`, `alertmanagerMain`, UWM `prometheus`, or
`thanosRuler`. Directly scaling their StatefulSets is therefore fought by the
operators. Directly scaling `cluster-monitoring-operator` is also insufficient:
its Deployment is CVO-owned (`ownerReferences` points to ClusterVersion
`version`). The supported CVO `spec.overrides[].unmanaged` mechanism is required
for that one Deployment during the short copy window.

Capture CVO overrides, operators, operands, retention and storage. Preserve the
entire override array because unrelated entries may coexist:

```bash
oc-sa altus get clusterversion/version -o json |
  jq '{spec:{overrides:(.spec.overrides // null)}}' > "$EVIDENCE/cvo-before.json"
for NS in openshift-monitoring openshift-user-workload-monitoring; do
  oc-sa altus -n "$NS" get deployment,statefulset -o json |
    jq '{items:[.items[] | {kind,metadata:{name:.metadata.name,namespace:.metadata.namespace},
         spec:{replicas:.spec.replicas}}]}' > "$EVIDENCE/$NS-replicas-before.json"
done
oc-sa altus -n openshift-monitoring get prometheus/k8s alertmanager/main -o json \
  > "$EVIDENCE/platform-monitoring-before.json"
oc-sa altus -n openshift-user-workload-monitoring get prometheus/user-workload \
  thanosruler/user-workload -o json > "$EVIDENCE/uwm-monitoring-before.json"
oc-sa altus -n openshift-monitoring get configmap cluster-monitoring-config \
  -o jsonpath='{.data.config\.yaml}' > "$EVIDENCE/cluster-monitoring-config-before.yaml"
oc-sa altus -n openshift-user-workload-monitoring get configmap user-workload-monitoring-config \
  -o jsonpath='{.data.config\.yaml}' > "$EVIDENCE/uwm-config-before.yaml"
```

Expected operator and operand replicas are all 1; current retention is 15d for
platform Prometheus, 120h for Alertmanager and 7d for UWM Prometheus/Thanos
Ruler. Abort on unexpected counts or missing retention evidence. Do not change
retention or the monitoring ConfigMaps.

Quiesce exactly in this order:

1. Add only
   `{group: apps, kind: Deployment, namespace: openshift-monitoring,
   name: cluster-monitoring-operator, unmanaged: true}` to the existing CVO
   override array with a merge generated from `cvo-before.json`. Do not replace
   other overrides. Verify the entry is present.
2. Scale `deployment/cluster-monitoring-operator` in `openshift-monitoring` to
   zero. Wait for its pod to disappear and verify replicas remain zero for two
   minutes; recreation indicates the override did not take effect and is an
   abort condition.
3. Scale `deployment/prometheus-operator` in
   `openshift-user-workload-monitoring` to zero. CMO is already stopped, so it
   cannot recreate this operand controller.
4. Scale `statefulset/alertmanager-main`, then `statefulset/prometheus-k8s` in
   `openshift-monitoring` to zero. Scale
   `statefulset/thanos-ruler-user-workload`, then
   `statefulset/prometheus-user-workload` in
   `openshift-user-workload-monitoring` to zero. Wait for all pods and relevant
   VolumeAttachments to disappear. Other monitoring Deployments are not
   persistent writers and need not be scaled.

Executable form of that sequence:

```bash
jq '.spec.overrides = (((.spec.overrides // []) + [
  {group:"apps",kind:"Deployment",namespace:"openshift-monitoring",
   name:"cluster-monitoring-operator",unmanaged:true}
]) | unique)' "$EVIDENCE/cvo-before.json" > "$EVIDENCE/cvo-unmanaged-patch.json"
oc-sa altus patch clusterversion/version --type=merge \
  --patch-file="$EVIDENCE/cvo-unmanaged-patch.json"
oc-sa altus get clusterversion/version -o json | jq '.spec.overrides'
oc-sa altus -n openshift-monitoring scale deployment/cluster-monitoring-operator --replicas=0
oc-sa altus -n openshift-monitoring wait --for=delete pod \
  -l app=cluster-monitoring-operator --timeout=10m
sleep 120
test "$(oc-sa altus -n openshift-monitoring get deployment/cluster-monitoring-operator -o jsonpath='{.spec.replicas}')" = 0
oc-sa altus -n openshift-user-workload-monitoring scale deployment/prometheus-operator --replicas=0
oc-sa altus -n openshift-monitoring scale statefulset/alertmanager-main --replicas=0
oc-sa altus -n openshift-monitoring scale statefulset/prometheus-k8s --replicas=0
oc-sa altus -n openshift-user-workload-monitoring scale statefulset/thanos-ruler-user-workload --replicas=0
oc-sa altus -n openshift-user-workload-monitoring scale statefulset/prometheus-user-workload --replicas=0
oc-sa altus -n openshift-monitoring wait --for=delete pod/alertmanager-main-0 pod/prometheus-k8s-0 --timeout=15m
oc-sa altus -n openshift-user-workload-monitoring wait --for=delete \
  pod/thanos-ruler-user-workload-0 pod/prometheus-user-workload-0 --timeout=15m
```

After all four archives are validated, restore in reverse dependency order:

1. With both operators still stopped, restore each captured StatefulSet replica
   count. Do not alter its PVC or retention. Wait for the restored data to pass
   the history/silence/rule checks.
2. Restore UWM `deployment/prometheus-operator` to its captured count and wait
   Ready; confirm it does not recreate or resize any PVC.
3. Restore `deployment/cluster-monitoring-operator` to its captured count.
4. Restore the original CVO override array exactly: if the before value was
   null, remove only `/spec/overrides`; otherwise merge the captured array back
   without adding, dropping or reordering unrelated entries. Confirm CVO is
   progressing and not degraded, and confirm the CMO Deployment remains at the
   captured/release-managed count.
5. Diff final operator/operand replicas, retention, storage and both ConfigMaps
   against the before files. Require historical platform/UWM queries,
   Alertmanager silences/state and Thanos rules before re-enabling writers or
   notifications.

Use the captured replica files rather than typing counts. The `while` loops
select only the four StatefulSets and two operators named above:

```bash
while IFS=$'\t' read -r NS KIND NAME REPLICAS; do
  oc-sa altus -n "$NS" scale "$KIND/$NAME" --replicas="$REPLICAS"
done < <(jq -r '.items[] | select(.kind == "StatefulSet") |
  [.metadata.namespace,.kind,.metadata.name,(.spec.replicas|tostring)] | @tsv' \
  "$EVIDENCE/openshift-monitoring-replicas-before.json" \
  "$EVIDENCE/openshift-user-workload-monitoring-replicas-before.json")
# Complete and record all historical data checks before restarting operators.
while IFS=$'\t' read -r NS NAME REPLICAS; do
  oc-sa altus -n "$NS" scale "deployment/$NAME" --replicas="$REPLICAS"
done < <(jq -r '.items[] | select(.kind == "Deployment" and
  ((.metadata.namespace == "openshift-monitoring" and .metadata.name == "cluster-monitoring-operator") or
   (.metadata.namespace == "openshift-user-workload-monitoring" and .metadata.name == "prometheus-operator"))) |
  [.metadata.namespace,.metadata.name,(.spec.replicas|tostring)] | @tsv' \
  "$EVIDENCE/openshift-monitoring-replicas-before.json" \
  "$EVIDENCE/openshift-user-workload-monitoring-replicas-before.json")
```

This restores both operators together; after the UWM operator is Ready and has
left the restored PVCs unchanged, restore only the CVO override array:

```bash
if jq -e '.spec.overrides == null' "$EVIDENCE/cvo-before.json" >/dev/null; then
  oc-sa altus patch clusterversion/version --type=json \
    -p '[{"op":"remove","path":"/spec/overrides"}]'
else
  oc-sa altus patch clusterversion/version --type=merge \
    --patch-file="$EVIDENCE/cvo-before.json"
fi
oc-sa altus get clusterversion/version -o json |
  jq '{overrides:.spec.overrides,conditions:.status.conditions}'
oc-sa altus -n openshift-monitoring rollout status deployment/cluster-monitoring-operator --timeout=10m
```

If the window is aborted, perform the same restoration even when an archive is
incomplete. Never solve a failed restore by deleting a WAL, reducing retention,
leaving an operator unmanaged, or starting with empty storage.

## B23-B24: representative recreation and managed-image assumptions

### B23 empty caches and queues

- Create `docling-serve-app` empty (5Gi), start the arbitrary-UID workload with
  `HF_HOME=/.cache/huggingface`, require `/health`, cold model/tokenizer
  download, and conversion of a known PDF to nonempty expected text/chunks.
  No old cache files may exist.
- In `gb23-empty-caches`, create a throwaway restic REST server, a 1Gi target
  `cache-probe-data`, and test credentials generated without terminal output.
  Do not point this probe at a production repository. Create ReplicationSource
  `cache-probe` with a unique manual trigger, `cacheStorageClassName:
  synology-iscsi-storage`, and require Successful backup plus a new Bound 1Gi
  VolSync cache PVC on that class.
- **Post-recreation only:** repeat with ReplicationSource
  `cache-probe-knowhere` and `cacheStorageClassName: lvms-knowhere`; require its
  1Gi cache, and only that cache, on `lvms-knowhere`. This is the R10 acceptance
  proof, not a pre-cutover Gate B test. Record generated cache names before
  deleting the namespace. Keep no test snapshots in production repositories.

Together with B02/B03/B05/B07/B09, this covers empty runner/DinD, cold model,
Valkey, four Redis, Docling and VolSync cache assumptions. Final cutover still
requires explicit drained-CI/queue evidence and successful independent backups
for every R cache owner.

### B24 and B19 managed images

Do not copy old image PVC contents and do not run a second import against a
production DataImportCron/DataSource. Clone each DataImportCron's DataVolume
template into the stated scratch namespace, rename it `import-iNN`, remove
hash-suffixed PVC expectations, set 30Gi Block/RWO on the scratch class, and
record source and resolved digest. Import and clean up one at a time.

- **B19 after registry passes:** expose the scratch registry only as ClusterIP
  Service `image-registry` port 5000 in `gb19-image-registry`. Prove O02 with a
  fully cached `qdrant/cosmere-knowledge-index` revision: require the unchanged
  manifest digest and every linked config/layer blob, and disable upstream
  fallback for the real pull. The RHEL DataImportCrons use `pullMethod: node`
  and direct `registry.redhat.io` sources, so their blobs were never cached in
  O02. For I04/I05/I06, clone the recorded RHEL10/RHEL9/RHEL8 upstream digest
  URLs from their DataImportCron templates; do not route them through scratch
  O02 or claim these imports prove restored registry blobs. Import `import-i04`,
  `import-i05`, then `import-i06`. Require DataVolume Succeeded, 30Gi Block Bound
  PVC, correct image size/format and digest. Boot each changed RHEL family in a
  throwaway isolated VM with no production network identity. The actual
  `rhel*-image-cron` → DataSource path remains a required post-restore check.
- **B24:** import I01 `centos-stream10-image-cron` from
  `docker://quay.io/containerdisks/centos-stream:10`, I02
  `centos-stream9-image-cron` from `docker://quay.io/containerdisks/centos-stream:9`,
  and I03 `fedora-image-cron` from
  `docker://quay.io/containerdisks/fedora:latest`, one at a time. Record old/new
  digests and investigate unexpected source-version changes. Require DataVolume
  Succeeded, correct 30Gi Block/class/format, DataSource-equivalent readiness in
  the scratch clone, and one isolated boot per CentOS/Fedora family. Do not
  expect the old hash-suffixed PVC names.

Pass is a compatible fresh reimport, not byte-for-byte equality with an old
managed image. Cleanup each `import-iNN` DataVolume/PVC and boot VM immediately
after its evidence row is complete; this is what caps B19's image request at
30Gi and B24 at 30Gi.

## Gate B and staged Knowhere sign-off checklist

B18a must pass before the rebuild. B18b is deliberately post-recreation and
must pass before the production return leg; it is not a pre-cutover requirement
for `lvms-knowhere` to exist.

- [ ] B00 has clean checks for every required local/B2 repository and recorded
      full-read scope for local-only high-value sources.
- [ ] D01-D11 all have isolated imports, reviewed SQL logs, schema lists and
      exact real row counts; Jellystat's additional import also passed.
- [ ] Coordinated V+DB sets passed together: Gitea V02+D01, Nextcloud V07+D06+
      NFS data, Paperless V08+V09+D07, Tymeslot V14+D09, Vaultwarden V15+D10.
- [ ] V01, V06, V10-V13 passed their real application reads; all 14 production
      V routes have ownership/content evidence. V03 separately passed as a
      Knowhere projects disaster-recovery restore on `lvms-vg1` scratch, with
      `/disk.img` and qemu ownership, and was not used in the production path.
- [ ] Current KNX V04+V05 passed ownership, NVRAM, firmware UUID, vTPM permall,
      Windows boot and no-BitLocker checks without the production dongle.
- [ ] B18a passed both disks together from `lvms-vg1` to iSCSI while the
      disposable VM ran altus → xandar. Xandar localnet, `retentionPolicy: keepSource`, retained
      identity/boot ID, continuous probes/writers, interim
      `synology-snapshot`, off-vg1 cache, and fresh active-PVC backup all passed.
- [ ] B18b passed the same still-running VM and both active iSCSI disks to
      `lvms-knowhere` while moving xandar → altus. Final snapshot/cache class,
      second fresh active-PVC backup, source retention, and observation evidence
      passed. No production session/VM was halted. The running-production root
      snapshot also passed encrypted off-node copy and scratch recovery checks;
      it is safety evidence only, not the live-migration mechanism.
- [ ] O02-O07 all passed full archive/extract comparison, numeric ownership,
      sparse/ACL/xattr/SELinux checks and their real registry/monitoring/OAuth
      validation. No history, silences, registry content or keys were trimmed.
- [ ] Representative R assumptions passed, including queue/CI drain criteria,
      cold caches, empty Redis/Valkey, runner MTU/overlay2 and VolSync cache
      class selection. All six I sources passed fresh CDI imports; shared
      DataImportCron/ImageStream verification is explicitly scheduled after
      the real registry restore.
- [ ] Registry and monitoring final-copy procedures have captured/restored
      exact management, pruner, CVO override and replica state in a reviewed
      rehearsal or are still unchecked and therefore block Gate C.
- [ ] Every batch row includes dates, source artifact or migration IDs,
      SHA-256/size where applicable, source/target PV/class/node, durations,
      command exit statuses, validation evidence, cleanup evidence and operator
      sign-off.
- [ ] Every scratch namespace, temporary source-restore PVC, VolumeSnapshot,
      throwaway VM, helper and RBAC grant was removed after evidence, except
      retained B18 sources explicitly awaiting reviewed cleanup. No source
      artifact/repository was pruned, no production source was automatically
      deleted, and no production writer was left stopped, unmanaged or
      suspended. Production Knowhere was never stopped.
