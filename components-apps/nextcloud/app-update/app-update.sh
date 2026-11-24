#!/bin/bash
# Weekly update of the Nextcloud app-store apps (custom_apps).
#
# Runs in the Nextcloud image itself, as www-data (uid 33), with the same
# volumes as the chart's nextcloud-app-cron CronJob: no Kubernetes API access.
# Backups are not checked here; the nightly psql dump (03:00) and the VolSync
# snapshots of nextcloud-app-nextcloud (03:10) run before this job (Sun 04:00)
# and have their own alerts.
#
# Order: occ status healthy -> tar custom_apps (keep KEEP_TARBALLS) ->
# occ app:update --all -> occ status healthy again -> before/after app list.
# Any failure exits non-zero, so the Job fails and NextcloudAppUpdateFailed fires.
set -euo pipefail

OCC=(php /var/www/html/occ --no-interaction)
APPS_DIR=/var/www/html/custom_apps
# On the nextcloud-app PVC (subPath root), outside the webroot /var/www/html.
BACKUP_DIR=/var/www/app-backups
KEEP_TARBALLS="${KEEP_TARBALLS:-4}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }
die() { log "ERROR: $*"; exit 1; }
on_exit() { [ "$1" -eq 0 ] || log "FAILED (exit $1)"; }
trap 'on_exit $?' EXIT

# occ status --output=json -> one key's value (true/false/number/string).
status_field() {
  printf '%s' "$1" | php -r '$s = json_decode(stream_get_contents(STDIN), true);
    if (!is_array($s) || !array_key_exists($argv[1], $s)) { exit(2); }
    echo is_bool($s[$argv[1]]) ? ($s[$argv[1]] ? "true" : "false") : $s[$argv[1]];' "$2"
}

check_status() {
  local when=$1 json installed maintenance dbupgrade version
  json=$("${OCC[@]}" status --output=json) || die "$when: occ status failed"
  installed=$(status_field "$json" installed) || die "$when: no 'installed' in occ status"
  maintenance=$(status_field "$json" maintenance) || die "$when: no 'maintenance' in occ status"
  dbupgrade=$(status_field "$json" needsDbUpgrade) || die "$when: no 'needsDbUpgrade' in occ status"
  version=$(status_field "$json" versionstring) || version='?'
  log "$when: occ status version=$version installed=$installed maintenance=$maintenance needsDbUpgrade=$dbupgrade"
  [ "$installed" = true ] || die "$when: Nextcloud is not installed"
  [ "$maintenance" = false ] || die "$when: maintenance mode is on"
  [ "$dbupgrade" = false ] || die "$when: needsDbUpgrade is true"
  # occ runs the code on the PVC (/var/www/html) with this image's PHP. The
  # image must match the running instance (CronJob image = app image, one
  # Renovate group); a skew means the app image moved without this job.
  [ "$version" = "$IMAGE_VERSION" ] \
    || die "$when: instance runs $version but this job's image is $IMAGE_VERSION; bump the CronJob image"
}

IMAGE_VERSION=$(php -r 'include "/usr/src/nextcloud/version.php"; echo $OC_VersionString;') \
  || die "cannot read the image's Nextcloud version"

# Enabled + disabled apps as "app version" lines, sorted.
app_versions() {
  "${OCC[@]}" app:list --output=json | php -r '$l = json_decode(stream_get_contents(STDIN), true);
    if (!is_array($l)) { exit(2); }
    foreach (["enabled", "disabled"] as $k) { foreach (($l[$k] ?? []) as $a => $v) {
      echo $a, " ", ($v === null || $v === "" ? "-" : $v), "\n"; } }' | sort
}

log "nextcloud-app-update starting (uid $(id -u))"
[ "$(id -u)" = 33 ] || die "must run as www-data (uid 33)"

# 1. Preflight
check_status preflight
before=$(app_versions) || die "occ app:list failed"

# 2. Rollback tarball of custom_apps, newest KEEP_TARBALLS kept
mkdir -p "$BACKUP_DIR"
tarball="$BACKUP_DIR/custom_apps-$(date '+%Y%m%d-%H%M%S').tgz"
[ ! -e "$tarball" ] || die "$tarball already exists"
tar -C "$(dirname "$APPS_DIR")" -czf "$tarball.tmp" "$(basename "$APPS_DIR")" \
  || { rm -f "$tarball.tmp"; die "tar of $APPS_DIR failed"; }
tar -tzf "$tarball.tmp" >/dev/null || { rm -f "$tarball.tmp"; die "tarball does not list"; }
mv "$tarball.tmp" "$tarball"
log "backup: $tarball ($(du -h "$tarball" | cut -f1))"

mapfile -t tarballs < <(find "$BACKUP_DIR" -maxdepth 1 -type f -printf '%f\n' \
  | grep -E '^custom_apps-[0-9]{8}-[0-9]{6}\.tgz$' | sort -r)
for old in "${tarballs[@]:$KEEP_TARBALLS}"; do
  rm -f -- "${BACKUP_DIR:?}/${old:?}"
  log "pruned: $BACKUP_DIR/$old"
done

# 3. Update. The app store only offers releases compatible with the running
# Nextcloud version.
log "offered updates:"
"${OCC[@]}" app:update --showonly || die "occ app:update --showonly failed"
"${OCC[@]}" app:update --all || die "occ app:update --all failed"

# 4. Postflight
check_status postflight
after=$(app_versions) || die "occ app:list failed"

log "app versions (before -> after):"
join -a1 -a2 -e '-' -o '0,1.2,2.2' <(printf '%s\n' "$before") <(printf '%s\n' "$after") \
  | awk '{ mark = ($2 != $3) ? "  UPDATED" : ""; printf "  %-28s %-14s -> %-14s%s\n", $1, $2, $3, mark }'
log "nextcloud-app-update done"
