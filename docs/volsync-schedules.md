# VolSync backup schedules

Every `ReplicationSource` built from `templates/volsync/{rest,backblaze}`
(here and in private-ops) gets its own start slot. The templates only carry a
placeholder (`2 7 * * *` rest, `6 7 * * *` b2); a consumer without its own
slot shows up at 07:02/07:06 UTC.

## Why

Until 05.11.2026 about 55 RSes started at 05:00 UTC together. Each Synology
iSCSI source (`volumeSnapshotClassName: synology-snapshot`) makes the
Synology CSI create a LUN cloned from a snapshot; that many CreateVolume calls
at once ran into `DeadlineExceeded`, and the resulting iSCSI LUN mismatch left
mover pods stuck for two hours (kanban t_639fbb2e).

## Scheme (all times UTC, VolSync evaluates cron in UTC)

- Window 05:00-06:59, after the 03:00 `psql-backup`/`mariadb-backup` dumps.
- **Synology iSCSI clone sources:** minutes divisible by 4 (05:00, 05:04, ...),
  one RS per slot. A clone sync takes 1-2 min alone (2-7 min under the old
  contention), so 4 min keeps at most one CreateVolume in flight. 30 slots.
- **All other sources** (lvms-vg1/lvms-knowhere snapshots, NFS, local
  `Direct`): odd minutes, one RS per slot.
- **Rest legs first (05:xx), b2 legs after (06:xx)**, so the local and the
  off-site sync of the same PVC never run together.
- Ranges: home-ops iSCSI rest 05:00-05:36, private-ops iSCSI 05:40-06:08,
  home-ops iSCSI b2 06:12-06:28. Free iSCSI slots: 06:32-06:56. Free other
  slots: 05:39, 05:43-05:59, 06:19-06:39, 06:43-06:59.
- A hash of the name was considered and rejected: with ~23 iSCSI sources in
  120 minutes, hash slots collide or land within a few minutes of each other
  almost surely, which is exactly the overload this avoids.

Not in the window on purpose (own patch files, unchanged): nextcloud
`data-backup`/`data-backup-b2` 03:10 and `data-nfs-backup` 03:15 (Nextcloud
maintenance window), taxbuddy `valkey-backup` 01:30 and `owner-profile-backup`
01:45, ingestbuddy `valkey-backup` 01:50.

## Adding a source

Pick the next free slot of the right grid, add it to the table, and patch it in
the volume's `backup/<volume>/kustomization.yaml`:

```yaml
  # Backup slot 06:32 UTC (Synology iSCSI clone grid), see docs/volsync-schedules.md.
  - target:
      group: volsync.backube
      version: v1alpha1
      kind: ReplicationSource
      name: backup
    patch: |-
      - op: replace
        path: /spec/trigger/schedule
        value: "32 6 * * *"
```

## Slots

| UTC | Grid | ReplicationSource | Repo |
|-----|------|-------------------|------|
| 05:00 | iSCSI clone | `gitea/database-backup` | home-ops |
| 05:01 | other | `actual-budget/data-backup` | home-ops |
| 05:03 | other | `gitea/data-backup` | home-ops |
| 05:04 | iSCSI clone | `immich/database-backup` | home-ops |
| 05:05 | other | `happier/data-backup` | home-ops |
| 05:07 | other | `immich/library-backup` | home-ops |
| 05:08 | iSCSI clone | `ingestbuddy/database-backup` | home-ops |
| 05:09 | other | `influxdb3/data-backup` | home-ops |
| 05:11 | other | `knowhere/projects-backup` | home-ops |
| 05:12 | iSCSI clone | `litellm/database-backup` | home-ops |
| 05:13 | other | `knx-ets/persistent-state-backup` | home-ops |
| 05:15 | other | `knx-ets/volume-backup` | home-ops |
| 05:16 | iSCSI clone | `nextcloud/database-backup` | home-ops |
| 05:17 | other | `paperless/data-backup` | home-ops |
| 05:19 | other | `paperless/export-backup` | home-ops |
| 05:20 | iSCSI clone | `paperless/database-backup` | home-ops |
| 05:21 | other | `paperless/media-backup` | home-ops |
| 05:23 | other | `paperless-mcp/data-backup` | home-ops |
| 05:24 | iSCSI clone | `taxbuddy/database-backup` | home-ops |
| 05:25 | other | `qdrant/crawl-progress-backup` | home-ops |
| 05:27 | other | `qdrant/data-backup` | home-ops |
| 05:28 | iSCSI clone | `tymeslot/database-backup` | home-ops |
| 05:29 | other | `qdrant/snapshots-backup` | home-ops |
| 05:31 | other | `resilio-sync/config-backup` | home-ops |
| 05:32 | iSCSI clone | `vaultwarden/database-backup` | home-ops |
| 05:33 | other | `taxbuddy-hermes/data-backup` | home-ops |
| 05:35 | other | `tymeslot/data-backup` | home-ops |
| 05:36 | iSCSI clone | `vehicle-pipeline/database-backup` | home-ops |
| 05:37 | other | `vaultwarden/config-backup` | home-ops |
| 05:40 | iSCSI clone | `jellystat/database-backup` | private-ops |
| 05:41 | other | `media-vpn/qbittorrent-backup` | private-ops |
| 05:44 | iSCSI clone | `media/audiobookshelf-backup` | private-ops |
| 05:48 | iSCSI clone | `media/overseerr-backup` | private-ops |
| 05:52 | iSCSI clone | `media/prowlarr-backup` | private-ops |
| 05:56 | iSCSI clone | `media/radarr-backup` | private-ops |
| 06:00 | iSCSI clone | `media/readarr-backup` | private-ops |
| 06:01 | other | `actual-budget/data-backup-b2` | home-ops |
| 06:03 | other | `happier/data-backup-b2` | home-ops |
| 06:04 | iSCSI clone | `media/sonarr-backup` | private-ops |
| 06:05 | other | `paperless/data-backup-b2` | home-ops |
| 06:07 | other | `paperless/export-backup-b2` | home-ops |
| 06:08 | iSCSI clone | `media/sonarr-anime-backup` | private-ops |
| 06:09 | other | `paperless/media-backup-b2` | home-ops |
| 06:11 | other | `paperless-mcp/data-backup-b2` | home-ops |
| 06:12 | iSCSI clone | `immich/database-backup-b2` | home-ops |
| 06:13 | other | `resilio-sync/config-backup-b2` | home-ops |
| 06:15 | other | `tymeslot/data-backup-b2` | home-ops |
| 06:16 | iSCSI clone | `nextcloud/database-backup-b2` | home-ops |
| 06:17 | other | `vaultwarden/config-backup-b2` | home-ops |
| 06:20 | iSCSI clone | `paperless/database-backup-b2` | home-ops |
| 06:24 | iSCSI clone | `tymeslot/database-backup-b2` | home-ops |
| 06:28 | iSCSI clone | `vaultwarden/database-backup-b2` | home-ops |
| 06:41 | other | `media-vpn/qbittorrent-backup-b2` | private-ops |
