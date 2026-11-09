#!/usr/bin/env bash
# validate_manifests.sh — single local/CI entry point for static validation of
# this repo's Kubernetes manifests.
#
# Phases:
#   0. yamllint over the target tree (config: .yamllint); --skip-yamllint
#      leaves it to CI's separate yamllint job
#   1. kustomize build --enable-helm for every build target (see below)
#   2. kubeconform schema validation of the rendered output (-strict,
#      missing schemas ignored; upstream Kubernetes schemas plus the
#      datreeio/CRDs-catalog for CRDs)
#   3. kube-linter security/best-practice lint of the rendered output
#      (config: .kube-linter.yaml at the repo root)
#
# Build targets: by default only top-level kustomizations, i.e. directories
# that no other kustomization under the target directory pulls in through
# `resources`, `components` or `bases` (local paths, resolved relative to the
# referencing directory). An included directory is rendered, schema-checked
# and linted as part of its parent's output. Directories containing a
# .skip_validation marker file are never built and don't count as parents,
# so a directory included only by such directories is still built. --all
# builds every kustomization directory (nightly CI run).
#
# Builds run in parallel (-j, default: CPU count). Targets that render the
# same helmCharts directory share one worker and run one after another:
# kustomize pulls a missing chart into that directory's charts/, and two
# concurrent pulls into the same place collide. Paths must not contain tabs
# or newlines.
#
# All tools are pinned in mise.toml (run `mise install` first); yq parses
# the kustomization files to find the build targets.
set -uo pipefail

ERROR_COLOR='\033[0;31m'
NO_COLOR='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$(pwd)"
SKIP_SCHEMA=0
SKIP_LINT=0
SKIP_YAMLLINT=0
BUILD_ALL=0
JOBS=""
SCHEMA_LOCATION='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Validate yamllint, kustomize builds, kubeconform schemas and kube-linter
checks for the kustomizations under the target directory.

Options:
  -d, --directory DIR   Base directory to scan for kustomization.yaml
                        files (default: current working directory)
      --all             Build every kustomization directory, not only the
                        top-level ones
  -j, --jobs N          Parallel builds (default: number of CPUs)
      --skip-yamllint   Skip the yamllint phase
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
    -d | --directory | -j | --jobs)
      if [ $# -lt 2 ]; then
        err "Error: $1 requires an argument"
        exit 2
      fi
      case "$1" in
        -d | --directory) TARGET_DIR="$2" ;;
        *) JOBS="$2" ;;
      esac
      shift 2
      ;;
    -d=* | --directory=*)
      TARGET_DIR="${1#*=}"
      shift
      ;;
    -j=* | --jobs=*)
      JOBS="${1#*=}"
      shift
      ;;
    --all)
      BUILD_ALL=1
      shift
      ;;
    --skip-yamllint)
      SKIP_YAMLLINT=1
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

if [ -z "$JOBS" ]; then
  JOBS="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
fi
case "$JOBS" in
  '' | *[!0-9]* | 0*)
    err "Error: --jobs needs a positive integer, got '$JOBS'"
    exit 2
    ;;
esac

if [ "$SKIP_YAMLLINT" -eq 0 ] && ! command -v yamllint >/dev/null 2>&1; then
  err "yamllint not found — install repo tools with: mise install (or pass --skip-yamllint)"
  exit 1
fi

if ! command -v yq >/dev/null 2>&1; then
  err "yq not found — install repo tools with: mise install"
  exit 1
fi

if command -v kustomize >/dev/null 2>&1; then
  KUSTOMIZE_FLAVOUR=kustomize
elif command -v oc >/dev/null 2>&1; then
  KUSTOMIZE_FLAVOUR=oc
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

# Phase 0: yamllint
if [ "$SKIP_YAMLLINT" -eq 0 ]; then
  echo "Running yamllint..."
  if ! yamllint "$TARGET_DIR"; then
    err "yamllint failed"
    exit 1
  fi
  echo "yamllint passed."
  echo
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- Build plan -------------------------------------------------------------
# graph lines: K <dir> (kustomization dir), S <dir> (.skip_validation),
# R <kustomization file> <reference>, H <kustomization file> (has helmCharts).
find "$TARGET_DIR" -name "kustomization.yaml" -print0 | sort -z >"$WORK/kfiles"
if [ ! -s "$WORK/kfiles" ]; then
  echo "No kustomization.yaml found under $TARGET_DIR"
  exit 0
fi

: >"$WORK/graph"
while IFS= read -r -d '' kfile; do
  dir="${kfile%/*}"
  printf 'K\t%s\n' "$dir" >>"$WORK/graph"
  if [ -f "$dir/.skip_validation" ]; then
    printf 'S\t%s\n' "$dir" >>"$WORK/graph"
    echo "Skipping $dir (.skip_validation marker file)"
  fi
done <"$WORK/kfiles"

# shellcheck disable=SC2016 # yq expression, not shell
YQ_REFS='((.resources // []) + (.components // []) + (.bases // []) | .[] | "R\t" + filename + "\t" + .),
  (select(.helmCharts) | "H\t" + filename)'
if ! xargs -0 yq -N "$YQ_REFS" <"$WORK/kfiles" >>"$WORK/graph"; then
  err "Could not read the kustomization files (see the yq error above)"
  exit 1
fi

# Output: $WORK/targets ("<n>\t<dir>" per build target, in path order) and
# one line per worker group ("<n>\t<dir>[\t<n>\t<dir>...]") on stdout.
# shellcheck disable=SC2016 # awk program, not shell
PLAN_AWK='
function norm(p,    n, a, i, k, s, out) {
  # Lexical path normalisation: drop "." and empty segments, fold "x/..".
  n = split(p, a, "/")
  k = 0
  for (i = 1; i <= n; i++) {
    if (a[i] == "" || a[i] == ".") continue
    if (a[i] == ".." && k > 0 && s[k] != "..") { k--; continue }
    s[++k] = a[i]
  }
  out = substr(p, 1, 1) == "/" ? "/" : ""
  for (i = 1; i <= k; i++) out = out (i > 1 ? "/" : "") s[i]
  return out == "" ? "." : out
}
function dirof(f) { sub(/\/[^\/]*$/, "", f); return f }
function root(x) { while (uf[x] != x) x = uf[x]; return x }
function visit(d, t,    j, a, b) {
  if ((t, d) in seen) return
  seen[t, d] = 1
  rendered[d] = 1
  if (d in helm) {
    if (d in owner) {
      a = root(owner[d]); b = root(t)
      if (a != b) uf[b] = a
    } else owner[d] = t
  }
  for (j = 1; j <= nkid[d]; j++) visit(kid[d, j], t)
}
function add_target(d) { target[++nt] = d; uf[d] = d; visit(d, d) }
$1 == "K" { d = norm($2); path[d] = $2; order[++nk] = d; next }
$1 == "S" { skip[norm($2)] = 1; nskip++; next }
$1 == "H" { helm[norm(dirof($2))] = 1; next }
$1 == "R" {
  p = norm(dirof($2))
  c = substr($3, 1, 1) == "/" ? norm($3) : norm(p "/" $3)
  ref[p, c] = 1
  next
}
END {
  for (pc in ref) {
    split(pc, e, SUBSEP)
    if (!(e[1] in path) || !(e[2] in path) || e[1] == e[2]) continue
    kid[e[1], ++nkid[e[1]]] = e[2]
    if (!(e[1] in skip)) included[e[2]] = 1
  }
  for (i = 1; i <= nk; i++) {
    d = order[i]
    if (!(d in skip) && (all || !(d in included))) add_target(d)
  }
  # Safety net: anything not rendered by a target (e.g. a reference cycle,
  # which kustomize rejects) is built on its own so the error surfaces.
  for (i = 1; i <= nk; i++) {
    d = order[i]
    if (!(d in skip) && !(d in rendered)) add_target(d)
  }
  for (i = 1; i <= nt; i++) {
    d = target[i]
    print i "\t" path[d] > targets
    g = root(d)
    if (!(g in gid)) { gid[g] = ++ng; line[ng] = "" }
    line[gid[g]] = line[gid[g]] (line[gid[g]] == "" ? "" : "\t") i "\t" path[d]
  }
  for (i = 1; i <= ng; i++) print line[i]
  printf "%d %d %d %d\n", nk, nskip, nt, ng > stats
}'
: >"$WORK/targets"
if ! awk -F'\t' -v all="$BUILD_ALL" -v targets="$WORK/targets" -v stats="$WORK/stats" \
  "$PLAN_AWK" "$WORK/graph" >"$WORK/groups"; then
  err "Could not compute the build plan"
  exit 1
fi
read -r n_kust n_skip n_targets n_groups <"$WORK/stats"

echo
if [ "$BUILD_ALL" -eq 1 ]; then
  echo "Building all $n_targets kustomization directories ($n_skip skipped by marker) in $n_groups worker groups, $JOBS in parallel"
else
  echo "Building $n_targets top-level of $n_kust kustomization directories" \
    "($((n_kust - n_skip - n_targets)) rendered through a parent, $n_skip skipped by marker)" \
    "in $n_groups worker groups, $JOBS in parallel"
fi

# --- Phases 1-3: per target build + schema validation + lint ---------------
kbuild() {
  if [ "$KUSTOMIZE_FLAVOUR" = oc ]; then
    oc kustomize "$1" --enable-helm
  else
    kustomize build "$1" --enable-helm
  fi
}

# validate_dir <n> <dir>: writes the full log to $WORK/log.<n> and the error
# count to $WORK/rc.<n>.
validate_dir() {
  local n="$1" dir="$2" errs=0 build_output
  {
    echo
    echo "Validating $dir"
    echo
    if ! build_output="$(kbuild "$dir")"; then
      err "Error building $dir"
      errs=1
    else
      if [ "$SKIP_SCHEMA" -eq 0 ]; then
        if ! printf '%s\n' "$build_output" | kubeconform \
          -strict \
          -ignore-missing-schemas \
          -schema-location default \
          -schema-location "$SCHEMA_LOCATION" \
          -; then
          err "Schema validation failed for $dir"
          errs=$((errs + 1))
        fi
      fi

      if [ "$SKIP_LINT" -eq 0 ]; then
        if ! printf '%s\n' "$build_output" | kube-linter lint --config "$REPO_ROOT/.kube-linter.yaml" -; then
          err "kube-linter reported issues in $dir"
          errs=$((errs + 1))
        fi
      fi
    fi
  } >"$WORK/log.$n" 2>&1
  echo "$errs" >"$WORK/rc.$n"
}

# run_group "<n>\t<dir>[\t<n>\t<dir>...]": validates the group's targets in order.
run_group() {
  local -a f
  local i
  IFS=$'\t' read -r -a f <<<"$1"
  for ((i = 0; i + 1 < ${#f[@]}; i += 2)); do
    validate_dir "${f[i]}" "${f[i + 1]}"
  done
}

export WORK REPO_ROOT KUSTOMIZE_FLAVOUR SKIP_SCHEMA SKIP_LINT SCHEMA_LOCATION ERROR_COLOR NO_COLOR
export -f err kbuild validate_dir run_group
# Exit status is collected from the rc files, not from xargs.
if [ "$n_targets" -gt 0 ]; then
  # shellcheck disable=SC2016 # $1 expands in the worker shell
  tr '\n' '\0' <"$WORK/groups" |
    xargs -0 -n 1 -P "$JOBS" bash -c 'set -uo pipefail; run_group "$1"' run_group || true
fi

errors=0
failed=""
while IFS=$'\t' read -r n dir; do
  cat "$WORK/log.$n" 2>/dev/null
  if ! read -r count <"$WORK/rc.$n" 2>/dev/null; then
    err "No result for $dir (worker died?)"
    count=1
  fi
  if [ "$count" -ne 0 ]; then
    errors=$((errors + count))
    failed="$failed  $dir"$'\n'
  fi
done <"$WORK/targets"

echo
if [ "$errors" -ne 0 ]; then
  err "$errors errors occurred, see logs. Failed:"
  printf '%s' "$failed" >&2
  exit 1
else
  echo "Manifests successfully validated!"
fi
