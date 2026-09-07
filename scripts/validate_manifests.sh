#!/usr/bin/env bash
# validate_manifests.sh — single local/CI entry point for static validation of
# this repo's Kubernetes manifests.
#
# Phases:
#   0. yamllint over the target tree (config: .yamllint)
#   1. kustomize build --enable-helm for every kustomization.yaml found
#   2. kubeconform schema validation of the rendered output (-strict,
#      missing schemas ignored; upstream Kubernetes schemas plus the
#      datreeio/CRDs-catalog for CRDs)
#   3. kube-linter security/best-practice lint of the rendered output
#      (config: .kube-linter.yaml at the repo root)
#
# Directories containing a .skip_validation marker file are skipped.
# All tools are pinned in mise.toml (run `mise install` first).
set -uo pipefail

ERROR_COLOR='\033[0;31m'
NO_COLOR='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$(pwd)"
SKIP_SCHEMA=0
SKIP_LINT=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Validate yamllint, kustomize builds, kubeconform schemas and kube-linter
checks for all kustomizations under the target directory.

Options:
  -d, --directory DIR   Base directory to scan for kustomization.yaml
                        files (default: current working directory)
      --skip-schema     Skip the kubeconform schema-validation phase
      --skip-lint       Skip the kube-linter phase
  -h, --help            Display this help text
EOF
}

err() {
  printf "${ERROR_COLOR}%s${NO_COLOR}\n" "$1" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    -d | --directory)
      if [ $# -lt 2 ]; then
        err "Error: $1 requires an argument"
        exit 2
      fi
      TARGET_DIR="$2"
      shift 2
      ;;
    -d=* | --directory=*)
      TARGET_DIR="${1#*=}"
      shift
      ;;
    --skip-schema)
      SKIP_SCHEMA=1
      shift
      ;;
    --skip-lint)
      SKIP_LINT=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      err "Error: unknown option: $1"
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v yamllint >/dev/null 2>&1; then
  err "yamllint not found — install repo tools with: mise install"
  exit 1
fi

if command -v kustomize >/dev/null 2>&1; then
  KUSTOMIZE_CMD=(kustomize build)
elif command -v oc >/dev/null 2>&1; then
  KUSTOMIZE_CMD=(oc kustomize)
else
  err "Neither kustomize nor oc found — install repo tools with: mise install"
  exit 1
fi

if [ "$SKIP_SCHEMA" -eq 0 ] && ! command -v kubeconform >/dev/null 2>&1; then
  err "kubeconform not found — install repo tools with: mise install (or pass --skip-schema)"
  exit 1
fi

if [ "$SKIP_LINT" -eq 0 ] && ! command -v kube-linter >/dev/null 2>&1; then
  err "kube-linter not found — install repo tools with: mise install (or pass --skip-lint)"
  exit 1
fi

errors=0

# Phase 0: yamllint
echo "Running yamllint..."
if ! yamllint "$TARGET_DIR"; then
  err "yamllint failed"
  exit 1
fi
echo "yamllint passed."
echo

# Phases 1-3: per-kustomization build + schema validation + lint
while IFS= read -r -d '' kfile; do
  dir="$(dirname "$kfile")"
  echo
  echo "Validating $dir"
  echo

  if [ -f "$dir/.skip_validation" ]; then
    echo "Skipping validation due to .skip_validation marker file"
    continue
  fi

  if ! build_output="$("${KUSTOMIZE_CMD[@]}" "$dir" --enable-helm)"; then
    err "Error building $dir"
    errors=$((errors + 1))
    continue
  fi

  if [ "$SKIP_SCHEMA" -eq 0 ]; then
    if ! printf '%s\n' "$build_output" | kubeconform \
      -strict \
      -ignore-missing-schemas \
      -schema-location default \
      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
      -; then
      err "Schema validation failed for $dir"
      errors=$((errors + 1))
    fi
  fi

  if [ "$SKIP_LINT" -eq 0 ]; then
    if ! printf '%s\n' "$build_output" | kube-linter lint --config "$REPO_ROOT/.kube-linter.yaml" -; then
      err "kube-linter reported issues in $dir"
      errors=$((errors + 1))
    fi
  fi
done < <(find "$TARGET_DIR" -name "kustomization.yaml" -print0)

echo
if [ "$errors" -ne 0 ]; then
  err "$errors errors occurred, see logs"
  exit 1
else
  echo "Manifests successfully validated!"
fi
