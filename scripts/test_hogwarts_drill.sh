#!/usr/bin/env bash
# CI job hogwarts-drill: render every stage of the Hogwarts restore drill
# (components-infra/hogwarts/drill) and assert the guard rails with
# scripts/check_hogwarts_drill.py, then prove the checker refuses known-bad
# variants (self-test). Needs kustomize and yq (mise.toml) and python3.
set -euo pipefail
cd "$(dirname "$0")/.."

DRILL=components-infra/hogwarts/drill
CHECK=scripts/check_hogwarts_drill.py
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stages=(restore fix-owner boot revert-snapshot revert-copy)
found=$(find "$DRILL" -mindepth 2 -maxdepth 2 -name kustomization.yaml -printf '%h\n' | xargs -n1 basename | sort | tr '\n' ' ')
expected=$(printf '%s\n' "${stages[@]}" | sort | tr '\n' ' ')
if [ "$found" != "$expected" ]; then
  echo "FAIL  drill stages on disk ($found) differ from the checked list ($expected)"
  exit 1
fi

rc=0
for stage in "${stages[@]}"; do
  kustomize build "$DRILL/$stage" | yq -o=json -I=0 '.' >"$work/$stage.jsonl"
  python3 -I "$CHECK" "$stage" <"$work/$stage.jsonl" || rc=1
  sed "s/^/$stage\t/" "$work/$stage.jsonl" >>"$work/all.tsv"
done
python3 -I "$CHECK" --self-test <"$work/all.tsv" || rc=1
exit "$rc"
