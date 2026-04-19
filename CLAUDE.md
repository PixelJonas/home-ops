# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

A GitOps-managed home lab Kubernetes/OpenShift cluster. All cluster state is declared here and reconciled by ArgoCD using the app-of-apps pattern. Secrets are sourced from Doppler via External Secrets Operator and never committed to git.

## Validation

```bash
# Validate all Kustomize builds
./scripts/validate_manifests.sh

# Validate a specific directory
./scripts/validate_manifests.sh -d components-apps/immich

# Enforce strict schema validation
./scripts/validate_manifests.sh -s

# Lint YAML
yamllint .
```

CI runs `yamllint` and `kustomize build` validation on every push/PR via `.github/workflows/validate-manifests.yaml`.

## Repository Structure

```
bootstrap/
  initial/          # One-time ArgoCD installation manifests
  base/             # App-of-apps ApplicationSets pointing to components-*
  overlays/local.home/  # Cluster-specific values (values-infra.yaml, values-apps.yaml)
components-infra/   # Infrastructure: cert-manager, CSI, monitoring, secrets, RBAC, etc.
components-apps/    # User applications: immich, paperless, n8n, vaultwarden, etc.
scripts/            # validate_manifests.sh and utilities
templates/          # Reusable manifests (e.g., replication destination)
```

## Architecture

**Deployment flow:** `bootstrap/initial/` → ArgoCD is installed → ArgoCD reads `bootstrap/base/` → two ApplicationSets discover and deploy everything under `components-infra/` and `components-apps/`.

**Sync waves** control ordering: wave 1 = core infra (operators, RBAC), wave 2 = storage/networking, wave 3+ = applications. Waves are set via the annotation `argocd.argoproj.io/sync-wave`.

**Each component** follows the same Kustomize pattern: a `base/` with the raw manifests and an `overlays/` (or `kustomization.yaml` at the root) that the ApplicationSet targets. Helm charts are pulled in via `HelmRepository` + `HelmRelease` resources or `kustomization.yaml` `helmCharts:` entries.

**Secrets** flow: Doppler project → External Secrets Operator `ClusterSecretStore` (`doppler-secret-store`) → `ExternalSecret` CRs per component → native Kubernetes `Secret` objects. Never store secret values in this repo.

**Auto-sync is disabled** for applications (manual sync preferred); infrastructure components may have auto-sync enabled. `autoprune: false` is the default to prevent accidental deletion.

## Key Technologies

| Tool | Role |
|------|------|
| ArgoCD | GitOps engine, app-of-apps |
| Kustomize | Manifest templating |
| Helm | Upstream chart packaging |
| External Secrets Operator | Pull secrets from Doppler |
| cert-manager | TLS via Let's Encrypt |
| Synology CSI / NFS provisioner | Persistent storage |
| Renovate | Automated dependency PRs |

## Adding New Apps

See **[docs/adding-apps.md](docs/adding-apps.md)** for the full guide covering:
- File/directory structure and kustomization layout
- bjw-s app-template v4.6.2 boilerplate (controllers, service, ingress, persistence)
- OpenShift-specific requirements (ingress class, sync-wave table)
- Pod security: when to add `anyuid` vs `privileged` SCC rolebinding
- Alpine DNS fix for musl-based images
- volsync backup pattern (MinIO + Backblaze B2) and required Doppler keys

## Conventions

- **Kustomize-first:** prefer `kustomization.yaml` overlays over raw YAML duplication.
- **Sync waves:** new infra components should set an appropriate `sync-wave` annotation; apps default to wave 3 or higher.
- **YAML style:** enforced by `.yamllint` — no `document-start` (`---`), line length warnings ignored, truthy values (`yes`/`no`) are allowed.
- **Renovate:** dependency updates are automated; commit messages follow `chore(deps): update …` convention. Do not manually bump image tags that Renovate tracks.
- **Secret scanning:** `.gitleaks.toml` defines patterns; never commit keys, tokens, or kubeconfigs (see `.gitignore`).
