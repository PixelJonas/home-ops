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

### Runner registration (repository-scoped)

Register as a **repository-scoped** self-hosted runner — never org-wide:

1. On GitHub: **PixelJonas/recordbuddy → Settings → Actions → Runners →
   New self-hosted runner** → **macOS / ARM64**.
2. Follow the download + config steps shown there, but configure with:

   ```bash
   ./config.sh \
     --url https://github.com/PixelJonas/recordbuddy \
     --token "$(doppler secrets get RECORDBUDDY_GHA_RUNNER_TOKEN --plain -p recordbuddy -c prd)" \
     --name mac-mini-m4-recordbuddy \
     --labels self-hosted,macOS,recordbuddy \
     --ephemeral \
     --unattended
   ```

   Labels must be exactly `self-hosted, macOS, recordbuddy` — workflows
   select this runner with `runs-on: [self-hosted, macOS, recordbuddy]`.

   `RECORDBUDDY_GHA_RUNNER_TOKEN` is the short-lived registration token
   from the GitHub UI (or `gh api`). **It expires after 1 hour** — register
   immediately after generating it, and never store it anywhere but
   Doppler.

   `--ephemeral` makes the runner take exactly one job and then
   de-register, so a compromised or failed build cannot poison the next
   one. With ephemeral mode the runner must be re-registered after each
   job — the launchd wrapper below handles this by re-running config
   before each start (see the plist note).

### Secrets: Doppler-injected, never a file on the Mac mini

All credentials come from Doppler project `recordbuddy`, config `prd`, and
are injected into the runner process environment via `doppler run --` at
service start. **No `.env` file, no plist `EnvironmentVariables` values,
no credentials written to disk on the Mac mini.**

The launchd plist wraps the runner in `doppler run`:

```
doppler run -p recordbuddy -c prd -- /opt/recordbuddy-gha-runner/run.sh
```

### launchd LaunchDaemon

`/Library/LaunchDaemons/com.pixeljonas.recordbuddy-gha-runner.plist`
(system-level LaunchDaemon so it starts at boot without a login; a
user-level `~/Library/LaunchDaemons` variant works if the machine is
always logged in as the runner user):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pixeljonas.recordbuddy-gha-runner</string>
    <key>UserName</key>
    <string>_recordbuddy-runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/doppler</string>
        <string>run</string>
        <string>-p</string>
        <string>recordbuddy</string>
        <string>-c</string>
        <string>prd</string>
        <string>--</string>
        <string>/opt/recordbuddy-gha-runner/run-ephemeral.sh</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/recordbuddy-gha-runner.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/recordbuddy-gha-runner.log</string>
</dict>
</plist>
```

Notes:

- The runner runs as a dedicated **unprivileged** user `_recordbuddy-runner`
  (create via `sysadminctl`/`dscl`, no admin group, no login shell needed
  beyond the keychain step below).
- `/opt/recordbuddy-gha-runner/run-ephemeral.sh` is a small wrapper that
  re-runs `config.sh --ephemeral` (pulling a fresh registration token via
  `gh api` using a PAT/FAT stored in Doppler) before invoking `run.sh`,
  because ephemeral runners de-register after every job. `KeepAlive: true`
  then restarts the cycle.
- Load with `sudo launchctl bootstrap system /Library/LaunchDaemons/com.pixeljonas.recordbuddy-gha-runner.plist`.

Log rotation via newsyslog — add to `/etc/newsyslog.d/recordbuddy-gha-runner.conf`:

```
# logfilename                                  [owner:group]           mode count size     when  flags
/var/log/recordbuddy-gha-runner.log            root:wheel              640  5     10000    *     GZ
```

### Xcode version pinning

Pin the Xcode version with `xcode-select` so builds are reproducible:

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -version
```

Record the exact pinned version here when the runner is set up:

- **Pinned Xcode version: TBD-by-owner**

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

### Manual bootstrap checklist (owner-only, cannot be automated)

- [ ] Create `_recordbuddy-runner` unprivileged user on the Mac mini
- [ ] Install Xcode (App Store or xip) and agree to license
      (`sudo xcodebuild -license accept`); pin via `xcode-select`, record
      version above
- [ ] First keychain unlock for the runner user (log in once as
      `_recordbuddy-runner` to create/unlock its login keychain)
- [ ] Import Apple Developer **signing certificate + private key** into the
      `_recordbuddy-runner` login keychain (export from the owner's
      keychain as `.p12`, import, set partition list via
      `security set-key-partition-list -S apple-tool:,apple: -s` so
      codesign can use it non-interactively)
- [ ] Grant the runner user access to the signing identities
      (`security set-key-partition-list` above, or keychain ACLs)
- [ ] Install the GitHub Actions runner under `/opt/recordbuddy-gha-runner`
      owned by `_recordbuddy-runner`
- [ ] Install the plist, bootstrap the LaunchDaemon, verify
      `tail -f /var/log/recordbuddy-gha-runner.log`
- [ ] Enable "Require approval for all outside collaborators" in repo
      settings before making the repo public

### Re-registration procedure

Registration tokens expire after 1 hour and ephemeral runners de-register
after each job, so re-registration is routine:

1. Remove the dead runner registration if it still shows on GitHub:
   **repo → Settings → Actions → Runners → … → Remove**, or
   `./config.sh remove --token <new-token>`.
2. Generate a fresh registration token (GitHub UI, or
   `gh api -X POST repos/PixelJonas/recordbuddy/actions/runners/registration-token`).
3. Update the Doppler key `RECORDBUDDY_GHA_RUNNER_TOKEN` (project
   `recordbuddy`, config `prd`) if the manual-token flow is being used.
4. Re-run `./config.sh` with the same flags as in the registration section.
5. Restart the service: `sudo launchctl kickstart -k system/com.pixeljonas.recordbuddy-gha-runner`.

---

## 3. Doppler keys to create

All keys go in Doppler project **`recordbuddy`**, config **`prd`**.
Populate them in one pass; the ARC scale set needs **none** of these (it
uses the existing shared `GHA_RUNNERS_*` app credentials in the
`homelab`/`infra-ops` projects).

| Key | Purpose | Where used |
|-----|---------|-----------|
| `RECORDBUDDY_GHA_RUNNER_TOKEN` | GitHub runner **registration token** — short-lived, expires after 1h; refresh per re-registration | Mac mini: `config.sh` invocation (manual / wrapper script) |
| `APPLE_ID` | Apple ID for notarization (`xcrun notarytool` / altool) | Mac mini runner env via `doppler run --` in signing/notarize workflow steps |
| `APPLE_APP_SPECIFIC_PASSWORD` | App-specific password for the Apple ID (simplest notarization auth) | Mac mini runner env via `doppler run --` |
| `APPSTORE_CONNECT_KEY_ID` | App Store Connect API key ID (alternative notarization auth) | Mac mini runner env via `doppler run --` |
| `APPSTORE_CONNECT_ISSUER_ID` | App Store Connect API issuer ID | Mac mini runner env via `doppler run --` |
| `APPSTORE_CONNECT_PRIVATE_KEY` | App Store Connect API private key (.p8 contents) | Mac mini runner env via `doppler run --` |
| `APPLE_TEAM_ID` | Apple Developer Team ID (codesign identity + notarytool `--team-id`) | Mac mini runner env via `doppler run --` |

Use **either** `APPLE_APP_SPECIFIC_PASSWORD` **or** the
`APPSTORE_CONNECT_*` trio for notarization — document which one the
workflows actually use once decided; keep both sets only if there's a
concrete reason.
