#!/bin/sh
# Weekly integrity check of the rest-server's restic repositories. One at a time,
# because of the data-movement budget. See cronjob.yaml for when and where it runs.
#
# Every secret file named *_REST_REPO_LOCAL holds one repo URL, so new apps are
# covered without touching this. The log names repos by key, never by URL.
#
# Inputs (environment): SECRETS_DIR, BUDGET_SECONDS, REPO_TIMEOUT_SECONDS,
# MAX_SNAPSHOT_AGE_SECONDS, SKIP_KEY_PATTERN (space-separated shell case
# patterns, optional).
set -u

now() { date -u +%s; }
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# Rotates through every pack in 8 weeks. The leading 0 of a week number would
# otherwise read as octal.
week=$(date -u +%V)
subset="$((${week#0} % 8 + 1))/8"
deadline=$(($(now) + BUDGET_SECONDS))
export RESTIC_PASSWORD_FILE="$SECRETS_DIR/RESTIC_PASSWORD"

failed=""
checked=0
for file in "$SECRETS_DIR"/*_REST_REPO_LOCAL; do
  [ -e "$file" ] || continue
  key=$(basename "$file")
  skip=""
  set -f # split the patterns on spaces, but don't glob them against files
  for pattern in ${SKIP_KEY_PATTERN:-}; do
    # shellcheck disable=SC2254 # a glob pattern on purpose
    case "$key" in
      $pattern) skip=1 ;;
    esac
  done
  set +f
  if [ -n "$skip" ]; then log "$key: skipped"; continue; fi

  left=$((deadline - $(now)))
  if [ "$left" -le 0 ]; then
    log "$key: NOT CHECKED, time budget used up"
    failed="$failed $key"
    continue
  fi
  limit=$REPO_TIMEOUT_SECONDS
  [ "$left" -lt "$limit" ] && limit=$left

  export RESTIC_REPOSITORY_FILE="$file"
  log "$key: restic check, subset $subset, limit ${limit}s"
  # SIGTERM lets restic release its exclusive lock. A killed check leaves a stale
  # lock, which then fails the next mover run.
  if ! timeout -s TERM -k 60 "$limit" restic check --read-data-subset="$subset" --no-cache; then
    log "$key: FAILED restic check"
    failed="$failed $key"
    continue
  fi

  # A repo can be intact and still have stopped receiving backups.
  latest=$(restic snapshots --no-lock --latest 1 --json |
    grep -o '"time":"[^"]*"' | cut -d'"' -f4 | cut -c1-19 | tr 'T' ' ' | sort | tail -n 1)
  if [ -z "$latest" ]; then
    log "$key: FAILED, no snapshots"
    failed="$failed $key"
    continue
  fi
  age=$(($(now) - $(date -u -d "$latest" +%s)))
  if [ "$age" -gt "$MAX_SNAPSHOT_AGE_SECONDS" ]; then
    log "$key: FAILED, newest snapshot is ${age}s old"
    failed="$failed $key"
    continue
  fi
  log "$key: ok, newest snapshot ${age}s old"
  checked=$((checked + 1))
done

if [ "$checked" -eq 0 ] && [ -z "$failed" ]; then
  log "FAILED: found no repositories in $SECRETS_DIR"
  exit 1
fi
if [ -n "$failed" ]; then
  log "FAILED:$failed"
  exit 1
fi
log "all $checked repositories ok"
