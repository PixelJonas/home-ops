# Hogwarts restore drill: scratch objects

Scratch objects for the `/home` restore drill (infra-ops spec section 10,
runbook [hogwarts-home-restore.md](../../../docs/runbooks/hogwarts-home-restore.md)
part A). **Not part of any ArgoCD Application** (`values-infra.yaml` points the
`hogwarts` app at `../base` only); nothing here reaches the cluster on merge.
infra-ops `mise run hogwarts:restore-drill` renders and applies these
directories one stage at a time, only after Jonas's go, and deletes the
namespace again at the end.

Everything lives in the namespace `hogwarts-restore`; nothing touches the
live namespace `hogwarts`.

| Directory | Stage | Objects |
|---|---|---|
| `restore/` | 2 | Namespace, restic ExternalSecret, PVC `home-test`, ReplicationDestination `home-restore`, helper ServiceAccount + `anyuid` RoleBinding |
| `fix-owner/` | 2 | Job `fix-owner` (`chown 107:107 disk.img`) |
| `boot/` | 3 | the production VM manifests from `../base`, patched: VM `hogwarts-restore` (`runStrategy: Halted`), root disk `hogwarts-restore-rootdisk`, cloud-init template, NetworkPolicies (egress **DNS + LiteLLM only**), SSH Service |
| `revert-snapshot/` | 5 | VolumeSnapshot `home-test-snap` |
| `revert-copy/` | 5 | PVC `home-test-revert`, Job `revert-copy` |

Hard-wired, checked by `scripts/check_hogwarts_drill.py` (CI job
`hogwarts-drill` and again by the drill script before every apply):

- every object is in `hogwarts-restore` (or is that Namespace);
- the VM is `hogwarts-restore`, `runStrategy: Halted`, its home disk is the
  PVC `home-test`, never `hogwarts-home`;
- the egress policy allows only cluster DNS and LiteLLM: no `ipBlock`
  (internet), no Nextcloud, Paperless, Qdrant or Gitea;
- no Secret, no bot token key, no Tailscale, no VolSync ReplicationSource.

`boot/` reuses `../base` on purpose: the drill boots the production VM and
cloud-init spec (`partition: auto` must reuse the restored ext4), and any
change to the base that would leak into the drill (a new object, a wider
egress rule) turns the CI check red.

The restore time (`restoreAsOf`) and the trigger token in
`restore/replicationdestination.yaml` are sentinels: applied unchanged, the RD
restores nothing (no snapshot exists before 1970). The drill script replaces
them with the time of its fresh backup and refuses to apply a render that
still carries them.
