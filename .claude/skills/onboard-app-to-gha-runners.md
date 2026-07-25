---
name: onboard-app-to-gha-runners
description: Onboard a new GitHub repo to the shared self-hosted GitHub Actions runner infrastructure on Sakaar
---

# Onboard a Repo to the Shared GitHub Actions Runners

Repeatable procedure to give a new GitHub repo self-hosted Actions runners
on Sakaar, using the **shared** GitHub App + namespace + credential design
(consolidated 2026-07-25 — see `docs/journal/altus-to-sakaar-migration.md`
for the history of why this replaced one-App-per-repo).

## Architecture (read this before touching anything)

- **One shared GitHub App** (`palbuddy-sakaar-gha-runners`, installed
  broadly across Jonas's repos) authenticates every runner scale-set —
  do **not** create a new GitHub App for a new repo.
- **One shared namespace** (`arc-runners`) hosts every runner scale-set's
  pods — do **not** create a new namespace.
- **One shared credential Secret** (`gha-runners-github-secret`, in
  `arc-runners`, sourced from Doppler `homelab/home` keys
  `GHA_RUNNERS_{APP_ID,INSTALLATION_ID,PRIVATE_KEY}` via the
  `doppler-cluster` ClusterSecretStore) — do **not** create new Doppler
  keys or a new ExternalSecret for a new repo.
- **One `RunnerScaleSet` Helm release per repo is still required** — this
  is an unavoidable GitHub constraint, not a gap in the design: a
  personal GitHub account (not an org) can only register self-hosted
  runners at the individual-repo level; there is no personal-account
  equivalent of an org-wide runner group. Each repo's scale-set still
  gets its own `githubConfigUrl`, its own `runnerScaleSetName`, and its
  own min/max scaling profile — only the namespace/App/credential are
  shared.
- The shared ARC controller (`actions-runner-controller`, namespace
  `actions-runner-system`) serves all scale-sets already; nothing to do
  there.

Reference implementation: `components-infra/gha-runner-scale-set-palbuddy/`
and `components-infra/gha-runner-scale-set-devland/` — copy the pattern
from whichever is closer to the new repo's needs.

## Prerequisites

1. **Confirm the GitHub App covers the target repo.** Check
   `https://github.com/settings/installations` under Jonas's account →
   `palbuddy-sakaar-gha-runners` → repository access. If it's scoped to
   "Only select repositories," add the new repo there (manual step, needs
   Jonas — GitHub App installation scope isn't something to change via
   API/Terraform in this setup). If it's already set to "All
   repositories," nothing to do here.
2. **Sakaar kubeconfig** — fetch `SAKAAR_OCP_CLAUDE_KUBECONFIG` from
   Doppler (`infra-ops/prd`) per this repo's CLAUDE.md safe-CLI pattern,
   to verify things land correctly later.
3. Decide the repo's runner scaling profile: `minRunners` (0 if the repo
   is low-traffic and cold-start latency is acceptable; 1+ to keep a warm
   runner), `maxRunners` (upper bound on concurrent jobs).

## Procedure

- [ ] **1. Create the component directory**
  `components-infra/gha-runner-scale-set-<repo>/base/` with three files:

  **`kustomization.yaml`**
  ```yaml
  apiVersion: kustomize.config.k8s.io/v1beta1
  kind: Kustomization

  commonAnnotations:
    argocd.argoproj.io/sync-options: SkipDryRunOnMissingResource=true

  resources:
    - privileged-clusterrolebinding.yaml

  helmCharts:
    - name: gha-runner-scale-set
      repo: oci://ghcr.io/actions/actions-runner-controller-charts
      version: 0.14.2   # match whatever version the other scale-sets are pinned to — check sibling components
      releaseName: arc-runner-set-<repo>
      namespace: arc-runners
      valuesFile: values.yaml
  ```

  **`values.yaml`**
  ```yaml
  githubConfigUrl: "https://github.com/PixelJonas/<repo>"
  githubConfigSecret: gha-runners-github-secret
  runnerScaleSetName: "arc-runner-set-<repo>"
  minRunners: 0
  maxRunners: 10
  controllerServiceAccount:
    namespace: actions-runner-system
    name: arc-gha-rs-controller
  containerMode: {}
  template:
    spec:
      containers:
        - name: runner
          image: ghcr.io/pixeljonas/sakaar-gha-runner:latest
          command: ["/home/runner/run.sh"]
          env:
            - name: RUNNER_ALLOW_RUNASROOT
              value: "1"
          securityContext:
            privileged: true
            runAsUser: 0
  ```

  **`privileged-clusterrolebinding.yaml`**
  ```yaml
  kind: ClusterRoleBinding
  apiVersion: rbac.authorization.k8s.io/v1
  metadata:
    name: arc-runner-set-<repo>-privileged
  subjects:
    - kind: ServiceAccount
      name: arc-runner-set-<repo>-gha-rs-no-permission
      namespace: arc-runners
  roleRef:
    apiGroup: rbac.authorization.k8s.io
    kind: ClusterRole
    name: system:openshift:scc:privileged
  ```

  No `namespace.yaml`, no `externalsecret.yaml` — those are shared
  (`components-infra/gha-runners-shared/`) and already deployed.

- [ ] **2. Wire it into ArgoCD** — add an entry to
  `bootstrap/overlays/sakaar/values-infra.yaml`, alongside the other
  `gha-runner-scale-set-*` entries:

  ```yaml
    gha-runner-scale-set-<repo>:
      annotations:
        argocd.argoproj.io/sync-wave: "3"
      destination:
        namespace: arc-runners
      source:
        path: components-infra/gha-runner-scale-set-<repo>/base
  ```

  This is `sakaar`-only — the shared runner infra doesn't exist on
  `local.home` (Altus) and isn't meant to; GitHub Actions runners are
  explicitly a Sakaar-only category (ephemeral/CI workload, not household
  software — see the governing principle in the migration journal).

- [ ] **3. Validate the kustomize build before pushing**
  ```bash
  kustomize build --enable-helm components-infra/gha-runner-scale-set-<repo>/base
  kustomize build --enable-helm bootstrap/overlays/sakaar
  ```

- [ ] **4. Point the target repo's workflows at the new runner** — in
  `<repo>/.github/workflows/*.yaml`, set:
  ```yaml
  runs-on: arc-runner-set-<repo>
  ```
  This must match `runnerScaleSetName` from step 1 exactly.

- [ ] **5. Commit and push home-ops.** ArgoCD's `sakaar` overlay has
  `autoSyncPrune: true` — it picks this up automatically, no manual sync
  needed (though `oc patch application cluster-config-manager -n
  openshift-gitops --type merge -p '{"operation":{"sync":{}}}'` forces an
  immediate refresh if you don't want to wait for the poll interval).

- [ ] **6. Verify live**
  ```bash
  export KUBECONFIG=<sakaar-kubeconfig>
  oc get application gha-runner-scale-set-<repo> -n openshift-gitops \
    -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
  oc get autoscalingrunnerset -n arc-runners
  ```
  Trigger one real workflow run in the target repo and confirm a runner
  pod actually appears in `arc-runners` and the job completes — don't
  just trust `Synced/Healthy` on the Application, that only confirms the
  scale-set object was created, not that a real job can run on it.

## Common mistakes (from the consolidation history)

- **Don't create a new namespace or a new ExternalSecret.** The whole
  point of this design is one namespace/secret shared across all
  scale-sets — a new per-repo namespace is exactly the anti-pattern that
  was fixed.
- **Don't assume the GitHub App is auto-installed on a new repo** unless
  it's explicitly set to "All repositories." Check first — a scale-set
  with no matching App installation will show `Synced` in ArgoCD but the
  runner will fail to register with GitHub, which only surfaces as a
  confusing auth error in the runner pod's logs, not an ArgoCD sync
  failure.
- **A personal GitHub account cannot share one `RunnerScaleSet` across
  repos** — don't try to point a new repo's workflows at an existing
  scale-set name to "save a step." Each repo needs its own scale-set
  object (still cheap — same namespace, same credentials), just not its
  own namespace/App/secret.
