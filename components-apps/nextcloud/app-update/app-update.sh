#!/usr/bin/env bash
# Weekly Nextcloud app-store app update (custom_apps), run by the
# nextcloud-app-update CronJob. Any failure exits non-zero (Job fails, alert
# NextcloudAppUpdateFailed). Order:
#   1. preflight: this night's psql-backup Job succeeded, ReplicationSources
#      data-backup + data-backup-b2 synced successfully this night, occ status
#      installed / no maintenance / no pending DB upgrade
#   2. occ app:update --showonly; nothing offered -> exit 0
#   3. keep only offered versions released >= MIN_RELEASE_AGE_DAYS ago (app
#      store API); younger ones wait for next week
#   4. tar custom_apps to /var/www/app-backups on the nextcloud-app PVC
#      (outside the webroot /var/www/html), keep the last KEEP_TARBALLS
#   5. occ app:update <app> one by one
#   6. postflight: occ status, status.php 200 via the Service, versions match
set -Eeuo pipefail

NS=nextcloud
DEPLOY=deploy/nextcloud-app
CONTAINER=nextcloud
STATUS_URL="http://nextcloud-app.${NS}.svc:8080/status.php"
BACKUP_DIR=/var/www/app-backups
MIN_AGE_DAYS="${MIN_RELEASE_AGE_DAYS:-3}"
KEEP="${KEEP_TARBALLS:-4}"

log() { echo "==> $(date '+%F %T %Z') $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
trap 'echo "ERROR: line $LINENO: $BASH_COMMAND failed" >&2' ERR

# occ as www-data (config.php owner); args passed as positional parameters,
# never re-parsed by a shell.
occ() {
  kubectl exec -n "$NS" "$DEPLOY" -c "$CONTAINER" -- \
    su -s /bin/sh www-data -c 'php occ --no-interaction "$@"' -- sh "$@"
}
# A plain command as www-data inside the Nextcloud container.
in_nc() {
  kubectl exec -n "$NS" "$DEPLOY" -c "$CONTAINER" -- \
    su -s /bin/sh www-data -c '"$@"' -- sh "$@"
}

check_status() {
  local json
  json="$(occ status --output=json)"
  python3 -c '
import json, sys
s = json.loads(sys.argv[1])
bad = []
if s.get("installed") is not True: bad.append("installed != true")
if s.get("maintenance") is not False: bad.append("maintenance != false")
if s.get("needsDbUpgrade") is not False: bad.append("needsDbUpgrade != false")
print("occ status: version=%s installed=%s maintenance=%s needsDbUpgrade=%s" % (
    s.get("versionstring"), s.get("installed"), s.get("maintenance"), s.get("needsDbUpgrade")))
sys.exit("occ status not healthy: " + ", ".join(bad) if bad else 0)
' "$json"
}

app_versions() {
  occ app:list --shipped=false --output=json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for state in ("enabled", "disabled"):
    for app, ver in sorted((d.get(state) or {}).items()):
        print(app, ver or "-")
'
}

# --- 1. preflight -----------------------------------------------------------
MIDNIGHT="$(date -d 'today 00:00' +%s)"
log "Preflight: backups since $(date -d "@$MIDNIGHT" '+%F %T %Z')"

kubectl get jobs -n "$NS" -o json | python3 -c '
import json, sys, calendar, time
midnight = int(sys.argv[1])
def epoch(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) if s else 0
jobs = [j for j in json.load(sys.stdin)["items"]
        if any(o.get("kind") == "CronJob" and o.get("name") == "database-psql-backup"
               for o in j["metadata"].get("ownerReferences", []))
        and epoch(j["status"].get("startTime")) >= midnight]
if not jobs:
    sys.exit("no database-psql-backup Job started this night")
ok = [j for j in jobs if j["status"].get("succeeded", 0) >= 1]
if not ok:
    sys.exit("database-psql-backup Job(s) of this night did not succeed: "
             + ", ".join(j["metadata"]["name"] for j in jobs))
for j in ok:
    print("psql-backup OK: %s completed %s" % (j["metadata"]["name"], j["status"].get("completionTime")))
' "$MIDNIGHT"

for rs in data-backup data-backup-b2; do
  kubectl get replicationsource -n "$NS" "$rs" -o json | python3 -c '
import json, sys, calendar, time
midnight = int(sys.argv[1])
r = json.load(sys.stdin)
st = r.get("status", {})
last = st.get("lastSyncTime")
res = (st.get("latestMoverStatus") or {}).get("result")
name, pvc = r["metadata"]["name"], r["spec"].get("sourcePVC")
if pvc != "nextcloud-app-nextcloud":
    sys.exit("%s backs up %s, expected nextcloud-app-nextcloud" % (name, pvc))
if not last or calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ")) < midnight:
    sys.exit("%s: no sync this night (lastSyncTime=%s)" % (name, last))
if res != "Successful":
    sys.exit("%s: latest mover result %s" % (name, res))
print("VolSync OK: %s lastSyncTime=%s result=%s" % (name, last, res))
' "$MIDNIGHT"
done

check_status

# --- 2. what does the app store offer? --------------------------------------
log "occ app:update --showonly"
SHOW="$(occ app:update --showonly)"
echo "$SHOW"
mapfile -t OFFERED < <(sed -n 's/^\([a-z0-9_]*\) new version available: \([^ ]*\)$/\1 \2/p' <<<"$SHOW")
if (( ${#OFFERED[@]} == 0 )); then
  log "All apps up to date, nothing to do"
  exit 0
fi

# --- 3. release age gate -----------------------------------------------------
NC_VERSION="$(occ status --output=json | python3 -c 'import json,sys; print(json.load(sys.stdin)["versionstring"])')"
API="https://apps.nextcloud.com/api/v1/platform/${NC_VERSION}/apps.json"
log "Release dates from $API"
APPS_JSON="$(mktemp)"
curl -fsS --retry 3 --max-time 120 -o "$APPS_JSON" "$API"

ELIGIBLE_TXT="$(printf '%s\n' "${OFFERED[@]}" | python3 -c '
import json, sys, calendar, time
apps = {a["id"]: a for a in json.load(open(sys.argv[1]))}
min_age = float(sys.argv[2]) * 86400
now = time.time()
for line in sys.stdin:
    app, ver = line.split()
    rel = next((r for r in apps.get(app, {}).get("releases", []) if r["version"] == ver), None)
    if rel is None:
        sys.exit("%s %s not found in the app store API" % (app, ver))
    created = calendar.timegm(time.strptime(rel["created"].split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S"))
    age = (now - created) / 86400
    if now - created >= min_age:
        print("eligible %s %s released %s (%.1f days)" % (app, ver, rel["created"], age), file=sys.stderr)
        print(app, ver)
    else:
        print("skip %s %s released %s (%.1f days < %s), next week" % (app, ver, rel["created"], age, sys.argv[2]), file=sys.stderr)
' "$APPS_JSON" "$MIN_AGE_DAYS")"
rm -f "$APPS_JSON"
mapfile -t ELIGIBLE < <(grep . <<<"$ELIGIBLE_TXT" || true)
if (( ${#ELIGIBLE[@]} == 0 )); then
  log "No offered release is old enough yet, nothing to do"
  exit 0
fi

BEFORE="$(app_versions)"

# --- 4. tarball of custom_apps ----------------------------------------------
TARBALL="custom_apps-$(date +%Y%m%d-%H%M%S).tgz"
log "Backing up custom_apps to $BACKUP_DIR/$TARBALL"
in_nc mkdir -p "$BACKUP_DIR"
in_nc tar -czf "$BACKUP_DIR/$TARBALL" -C /var/www/html custom_apps
in_nc tar -tzf "$BACKUP_DIR/$TARBALL" >/dev/null
in_nc ls -l "$BACKUP_DIR/$TARBALL"

LISTING="$(in_nc ls -1 "$BACKUP_DIR")"
mapfile -t TARBALLS < <(grep -E '^custom_apps-[0-9]{8}-[0-9]{6}\.tgz$' <<<"$LISTING" | sort || true)
if (( ${#TARBALLS[@]} > KEEP )); then
  for f in "${TARBALLS[@]:0:${#TARBALLS[@]}-KEEP}"; do
    log "Removing old tarball $BACKUP_DIR/$f"
    in_nc rm -f -- "$BACKUP_DIR/$f"
  done
fi

# --- 5. update app by app ----------------------------------------------------
declare -A WANT
for e in "${ELIGIBLE[@]}"; do
  read -r app ver <<<"$e"
  WANT[$app]="$ver"
  log "occ app:update $app (-> $ver)"
  occ app:update "$app"
done

# --- 6. postflight ------------------------------------------------------------
log "Postflight"
check_status
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$STATUS_URL")"
[[ "$code" == 200 ]] || die "status.php via the Service returned HTTP $code"
echo "status.php: HTTP $code"

AFTER="$(app_versions)"
echo
printf '%-20s %-14s %-14s\n' APP BEFORE AFTER
join -a1 -a2 -e - -o 0,1.2,2.2 <(sort <<<"$BEFORE") <(sort <<<"$AFTER") |
  while read -r app b a; do printf '%-20s %-14s %-14s%s\n' "$app" "$b" "$a" "$([[ $b != "$a" ]] && echo '  *')"; done
echo

fail=0
for app in "${!WANT[@]}"; do
  now="$(awk -v a="$app" '$1 == a {print $2}' <<<"$AFTER")"
  if [[ "$now" != "${WANT[$app]}" ]]; then
    echo "ERROR: $app is at ${now:-missing}, expected ${WANT[$app]}" >&2
    fail=1
  fi
done
(( fail == 0 )) || die "app versions after the update do not match"
log "Done: updated ${#WANT[@]} app(s); rollback tarball $BACKUP_DIR/$TARBALL"
