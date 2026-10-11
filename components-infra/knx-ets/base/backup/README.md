# knx-ets backup (VolSync restic, local-only)

Active since 2026-10-11 (home-ops #878): `./backup` is wired into
`../kustomization.yaml` and covers both PVCs (`volume` = root disk,
`persistent-state` = vTPM/EFI state) with daily 05:00 restic syncs to the
in-cluster restic-rest-server. Doppler keys
`KNX_ETS_VOLUME_REST_REPO_LOCAL` and `KNX_ETS_PERSISTENT_STATE_REST_REPO_LOCAL`
live in homelab/home. Local-only, no B2 by decision 2026-10-11.

Restore: `templates/replicationdestination.yaml` (manual trigger), see
docs/runbooks/nextcloud-restore.md for the procedure shape.

Note: the pre-restore July 2026 tars on the pmb-backup NAS share
(`knx-ets-knx-ets-backup/`: volume, persistent-state, win11 golden image)
are kept indefinitely per Jonas, 2026-10-11 — do not delete as "cleanup".
