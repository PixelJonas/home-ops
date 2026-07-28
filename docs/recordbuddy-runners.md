# RecordBuddy CI Runners

RecordBuddy (M0.5 milestone) needs two CI runner targets:

1. **ARC Linux scale set** on the Sakaar cluster — for everything that can
   run on Linux (lint, unit tests, non-Apple tooling). Managed by GitOps in
   this repo.
2. **macOS runner on the Mac mini (M4)** — for Swift/Xcode builds, code
   signing, notarization, and DMG packaging, which cannot run on Linux.
   **Not** managed by ARC/ArgoCD; set up manually per the security spec in
   section 2 below.

---

## 1. ARC Linux scale set (Sakaar)

### What it is

`components-infra/gha-runner-scale-set-recordbuddy/` is an ARC
`gha-runner-scale-set` (chart v0.14.2) registered in
`bootstrap/overlays/sakaar/values-infra.yaml` at sync-wave `3`, namespace
`arc-runners`:

- `githubConfigUrl`: `https://github.com/PixelJonas/recordbuddy`
- `runnerScaleSetName` / `releaseName`: `arc-runner-set-recordbuddy`
- `minRunners: 0`, `maxRunners: 3`
- Runs the shared image `ghcr.io/pixeljonas/sakaar-gha-runner:latest`
- Uses the **shared** GitHub App credentials via the existing
  `gha-runners-github-secret` ExternalSecret (Doppler keys
  `GHA_RUNNERS_APP_ID` / `GHA_RUNNERS_INSTALLATION_ID` /
  `GHA_RUNNERS_PRIVATE_KEY`, ClusterSecretStore `doppler-cluster`) — the same
  secret palbuddy / tax-agent / devland use.
- A privileged `ClusterRoleBinding`
  (`arc-runner-set-recordbuddy-privileged` → SA
  `arc-runner-set-recordbuddy-gha-rs-no-permission`) grants the runner pods
  the `privileged` SCC, matching the other scale sets.

No new Doppler keys are needed for this part — it rides the shared
`GHA_RUNNERS_*` GitHub App.

### REQUIRED MANUAL STEP: install the GitHub App on the repo

The shared GitHub App is installed per-repository. Until it is installed on
`PixelJonas/recordbuddy`, runner pods will fail to register and crash-loop.
This is a browser-only, owner-only step — it cannot be automated or done via
GitOps:

1. Go to **GitHub → Settings → Developer settings → GitHub Apps** (or the
   org/user settings page where the shared runner app lives).
2. Select the shared ARC runner app → **Install** / **Configure**.
3. Under **Repository access**, add `PixelJonas/recordbuddy` (or switch to
   "All repositories" if that is the standing policy — per-repo is
   preferred).
4. Save.

### Verification

After ArgoCD syncs the new component **and** the GitHub App is installed on
the repo:

```bash
oc get pods -n arc-runners | grep recordbuddy
oc get autoscalingrunnerset -n arc-runners
```

Expected: an `arc-runner-set-recordbuddy-*-listener` pod Running, and the
`AutoscalingRunnerSet` showing `MinRunners: 0`, `MaxRunners: 3`. Actual
runner pods (`arc-runner-set-recordbuddy-*-runner-*`) only appear while a
job is queued or running, since `minRunners` is 0.

In GitHub: **PixelJonas/recordbuddy → Settings → Actions → Runners** should
list the scale set `arc-runner-set-recordbuddy` once a workflow targets it
via `runs-on: arc-runner-set-recordbuddy`.

---

## 2. macOS runner on the Mac mini (M4)

### Purpose

Swift/Xcode builds, code signing, notarization, and DMG packaging require
macOS and cannot run on the Linux ARC runners. This runner is a classic
self-hosted GitHub Actions runner service on the Mac mini — **not** ARC,
not GitOps-managed — set up per the project's security spec.

### Shipped implementation (2026-07-27)

The runner is **declaratively managed by nix-darwin**, not set up by hand:
module `stacks/asgard/modules/github-runner.nix` in the **infra-ops** repo,
applied on asgard with `mise run asgard:switch`. What the module does:

- Downloads the official actions-runner tarball (pinned `2.336.0`,
  sha256-verified at install time) into
  `~/actions-runner-recordbuddy` on the primary user. Idempotent — skips
  when `run.sh` exists; version bumps = bump the pin, delete the install
  dir, re-switch.
- Runs it as a launchd **user agent** `org.nixos.github-runner-recordbuddy`
  (primary user, not a dedicated `_recordbuddy-runner` — the asgard stack
  has no service-user machinery; revisit if signing identities need user
  isolation in M1).
- Registers **repository-scoped** to `PixelJonas/recordbuddy` with labels
  exactly `self-hosted, macOS, recordbuddy` (GitHub adds `ARM64`), runner
  name `asgard-recordbuddy`, `--ephemeral --unattended --replace`.
- Fetches the short-lived registration token **via the primary user's `gh`
  CLI auth** (`gh api -X POST
  repos/PixelJonas/recordbuddy/actions/runners/registration-token`) on every
  (re)start. No runner token is stored anywhere — no Doppler key, no PAT on
  disk. Registration tokens expire after 1h, which is why they are fetched
  just-in-time.
- Ephemeral cycle: after each job GitHub de-registers the runner, the
  process exits 0, launchd restarts it (unconditional `KeepAlive` +
  `ThrottleInterval = 30` — `SuccessfulExit=false` does NOT work here
  because ephemeral exits are clean), and the wrapper re-registers with a
  fresh token. Verified end-to-end: job → exit → re-register → next job.

Operations:

```bash
# restart the agent (e.g. after fixing auth)
launchctl kickstart -k gui/$(id -u)/org.nixos.github-runner-recordbuddy
# logs
tail -f ~/Library/Logs/github-runner-recordbuddy.log
# verify registration
gh api repos/PixelJonas/recordbuddy/actions/runners --jq '.runners[] | {name,status,busy}'
```

Log files live under `~/Library/Logs/` and rotate with the user's standard
log handling — no newsyslog entry needed (user agent, not LaunchDaemon).

### Xcode version pinning

Pin the Xcode version with `xcode-select` so builds are reproducible:

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -version
```

Record the exact pinned version here when the runner is set up:

- **Pinned Xcode version: 26.6 (build 17F113)**, installed via
  `mas install 497799835` on 2026-07-27, `xcode-select` pointed at
  `/Applications/Xcode.app/Contents/Developer`, license accepted,
  first-launch components installed.

### SECURITY (non-negotiable)

The Mac mini runner executes code with access to signing identities and
notarization credentials. These rules are not optional:

- **Never execute fork-PR code.** Workflows that target this runner must
  trigger only on: `push` to protected branches, `workflow_dispatch`, and
  tags. Never `pull_request` (which runs the PR author's code) on
  workflows using this runner.
- **Repo setting: "Require approval for all outside collaborators"** (repo
  → Settings → Actions → General → Fork pull request workflows) must be
  enabled **before the repo goes public**.
- **Repository-scoped registration only.** Never register this runner at
  the organization level, never add it to shared runner groups.
- Ephemeral mode (`--ephemeral`) is mandatory — see above.
- Notarization credentials live only in Doppler and only enter the runner
  process via `doppler run --`.

### Manual bootstrap checklist (remaining, owner-only)

M1 needs **none** of these: the recorder fork is a SwiftPM package
(`app/MeetingTranscriber/Package.swift`, no `.xcodeproj`), and Command
Line Tools 26.6 (Swift 6.3.3) plus the `notarytool`/`stapler` shims are
already installed on asgard — `swift build` works today.

For M6 (public release / distribution to other Macs):

- [ ] Full Xcode install (App Store or xip), pin via `xcode-select`,
      record version above. Only needed for release tooling and
      Instruments-grade debugging, not for building.
- [ ] **Apple Developer Program membership** ($99/yr, owner account
      decision) → creates the `Developer ID Application` certificate
      required for Gatekeeper-clean distribution. Free Apple IDs only get
      local "Personal Team" development certs, which are fine for own
      machines (and recommended in M1 so TCC microphone/screen-recording
      permissions survive rebuilds — create via Xcode → Settings →
      Accounts when Xcode lands, or accept per-build re-prompts with
      ad-hoc signing).
- [ ] Import the Developer ID certificate into the primary user's login
      keychain and set the partition list via
      `security set-key-partition-list -S apple-tool:,apple: -s` so
      codesign can use it non-interactively
- [ ] Create the Apple notarization Doppler keys (see §3)
- [ ] Enable "Require approval for all outside collaborators" in repo
      settings before making the repo public

### Re-registration procedure

Re-registration is fully automatic (ephemeral mode + `gh` token fetch +
launchd `KeepAlive`) — nothing to do after jobs, reboots, or token expiry.
If the runner is stuck (e.g. `gh auth` broken), fix auth, then:
`launchctl kickstart -k gui/$(id -u)/org.nixos.github-runner-recordbuddy`.

---

## 3. Doppler keys to create

Owner decision (2026-07-27): reuse the existing **`homelab`** project,
config **`home`** (same place the shared `GHA_RUNNERS_*` ARC app
credentials live) instead of a dedicated `recordbuddy` project.

**No key is needed for runner registration** — the Mac mini runner fetches
tokens just-in-time via `gh` auth, and the ARC scale set rides the shared
`GHA_RUNNERS_*` app.

Needed only for M1 (signing/notarization) — none exist yet:

| Key | Purpose | Where used |
|-----|---------|-----------|
| `APPLE_ID` | Apple ID for notarization (`xcrun notarytool` / altool) | Mac runner env via `doppler run --` in signing/notarize workflow steps |
| `APPLE_APP_SPECIFIC_PASSWORD` | App-specific password for the Apple ID (simplest notarization auth) | Mac runner env via `doppler run --` |
| `APPSTORE_CONNECT_KEY_ID` | App Store Connect API key ID (alternative notarization auth) | Mac runner env via `doppler run --` |
| `APPSTORE_CONNECT_ISSUER_ID` | App Store Connect API issuer ID | Mac runner env via `doppler run --` |
| `APPSTORE_CONNECT_PRIVATE_KEY` | App Store Connect API private key (.p8 contents) | Mac runner env via `doppler run --` |
| `APPLE_TEAM_ID` | Apple Developer Team ID (codesign identity + notarytool `--team-id`) | Mac runner env via `doppler run --` |

Already present in `homelab`/`home`, reusable: `HUGGINGFACE_TOKEN` (model
mirror downloads, see recordbuddy §5.6).

Use **either** `APPLE_APP_SPECIFIC_PASSWORD` **or** the
`APPSTORE_CONNECT_*` trio for notarization — document which one the
workflows actually use once decided; keep both sets only if there's a
concrete reason.
