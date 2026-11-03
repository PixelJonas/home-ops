#!/usr/bin/env bash
# Runs promtool unit tests for PrometheusRule manifests. promtool reads plain
# rule files, so each test directory's rule is unwrapped from its CR first.
#
# Layout: <component>/prometheusrule.yaml + <component>/tests/*.test.yaml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status=0

while IFS= read -r test_dir; do
  rule="$(dirname "$test_dir")/prometheusrule.yaml"
  generated="$test_dir/rules.generated.yaml"
  # Keep what's under spec:, dedented by the two spaces of the CR's nesting.
  sed -n '/^spec:$/,$p' "$rule" | sed -e '1d' -e 's/^  //' >"$generated"
  promtool check rules "$generated" || status=1
  promtool test rules "$test_dir"/*.test.yaml || status=1
  rm -f "$generated"
done < <(find "$REPO_ROOT/components-infra" "$REPO_ROOT/components-apps" "$REPO_ROOT/templates" \
  -type d -name tests -print 2>/dev/null | while read -r d; do
    [ -f "$(dirname "$d")/prometheusrule.yaml" ] && printf '%s\n' "$d"
  done)

exit "$status"
