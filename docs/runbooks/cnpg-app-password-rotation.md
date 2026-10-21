# CNPG App-User Password Rotation Runbook

Rotating the Postgres app-user password of a CloudNativePG cluster whose
credential chain is **Doppler → External Secrets Operator → CNPG bootstrap
secret → app workloads**. Written from the live `taxbuddy-db` rotation on
2026-10-11 (Altus, CNPG v1.30.1); adapt names per app.

## Credential chain (know it before you start)

One Doppler key (e.g. `TAXBUDDY_POSTGRES_PASSWORD`, project `homelab`,
config `home`) fans out to two ESO-managed secrets in the app namespace:

- `<app>-db-app-secret` (keys `username` + `password`) — referenced by the
  CNPG cluster's `bootstrap.initdb.secret.name`. CNPG tracks it via
  `status.secretsResourceVersion.applicationSecretVersion` and reconciles
  the Postgres role password from it.
- `<app>-db-connection` (key `uri`) — the full `DATABASE_URL` assembled in
  the ExternalSecret template (`external-<app>-db-connection.yaml`).
  Consumed as env by every DB-touching workload.

So: **rotate the single Doppler key; never edit k8s secrets by hand.**

Consumers come in two pickup classes:

- **Deployments** reference the secret via `secretKeyRef` env — resolved at
  container start, so they need an explicit `oc rollout restart`.
- **CronJobs / hook Jobs** resolve env at each new pod — no action needed.

## Timing

There is an unavoidable auth-failure window between the Postgres role
flipping (as soon as CNPG reconciles the app secret) and the Deployment
pods restarting (they hold the old URI in env until then). Keep it to
minutes, and avoid scheduled CronJob windows so a cron pod doesn't start
into the gap (taxbuddy: avoid 05:15–06:30 Europe/Berlin).

## Procedure

All secret handling is hash/compare-only — never print a value.

1. **Generate** a URI-safe password (hex avoids URL-encoding pitfalls in
   the ESO `uri` template) and stash it in a chmod-600 temp file:

   ```bash
   openssl rand -hex 24 > /tmp/rot-pw && chmod 600 /tmp/rot-pw
   ```

2. **Update Doppler** (output suppressed; verify by hash, not by printing):

   ```bash
   doppler secrets set TAXBUDDY_POSTGRES_PASSWORD="$(tr -d '\n' < /tmp/rot-pw)" \
     -p homelab -c home > /dev/null 2>&1; echo "rc=$?"
   doppler secrets get TAXBUDDY_POSTGRES_PASSWORD --plain -p homelab -c home \
     | tr -d '\n' | sha256sum
   tr -d '\n' < /tmp/rot-pw | sha256sum   # must match
   ```

3. **Force-sync both ExternalSecrets** (default refreshInterval is 1h):

   ```bash
   oc annotate externalsecret <app>-db-app-secret <app>-db-connection \
     -n <ns> force-sync=$(date +%s) --overwrite
   ```

   Confirm `status.conditions[Ready]=True` and a fresh `refreshTime`.

4. **Verify the k8s secrets flipped** (compare in shell, print only YES/NO):

   ```bash
   NEW=$(tr -d '\n' < /tmp/rot-pw)
   APP=$(oc get secret <app>-db-app-secret -n <ns> -o jsonpath='{.data.password}' | base64 -d)
   [ "$APP" = "$NEW" ] && echo YES || echo NO
   # same for the uri key against the expected template string
   ```

5. **Make CNPG reconcile the role.** The operator did *not* react to the
   secret change on its own in the 2026-10-11 rotation (stale
   `applicationSecretVersion`, no operator log activity) — annotate the
   cluster to force a reconcile, then confirm the tracked version matches
   the secret's current `resourceVersion`:

   ```bash
   oc annotate cluster <app>-db -n <ns> force-reconcile=$(date +%s) --overwrite
   oc get cluster <app>-db -n <ns> \
     -o jsonpath='{.status.secretsResourceVersion.applicationSecretVersion}'
   oc get secret <app>-db-app-secret -n <ns> -o jsonpath='{.metadata.resourceVersion}'
   ```

6. **Auth-test with the new password before touching pods** — from an
   existing app pod, password piped via stdin, errors wrapped so a
   traceback can never echo a connection string:

   ```bash
   oc exec -i -n <ns> deploy/<some-app-deploy> -- python3 -c '
   import sys, psycopg
   pw = sys.stdin.read().strip()
   try:
       with psycopg.connect(host="<app>-db-rw.<ns>.svc", port=5432,
                            dbname="<db>", user="<user>", password=pw,
                            connect_timeout=10) as c:
           print("AUTH_OK", c.execute("select current_user").fetchone()[0])
   except Exception as e:
       print("AUTH_FAIL", type(e).__name__)
   ' < /tmp/rot-pw
   ```

7. **Restart every Deployment consumer immediately** (the failure window
   starts when the role flips): `oc rollout restart -n <ns> deploy/...`
   for each, then `oc rollout status` each one.

8. **Post-checks**: grep all service logs since restart for
   `password authentication failed` / connection errors (expect zero);
   confirm one normal workload cycle succeeds (e.g. a poll loop that
   queries the DB). CronJobs need nothing — but spot-check the next run of
   any job that passes the password around unusually (taxbuddy's
   `database-psql-backup` holds it as `POSTGRES_POSTGRES_PASSWORD` yet
   authenticates via local socket, so it works regardless).

9. **Cleanup**: `rm -f /tmp/rot-pw` (verify gone).

## Notes / gotchas

- No rollback in the old direction — the role password is replaced.
  Recovery from a botched step is simply re-setting the Doppler key and
  re-running from step 3.
- One shared app user means all services rotate together; there is no
  per-service rotation.
- If pods keep failing after the restart, check whether the workload reads
  the secret via a volume mount instead of env (none in taxbuddy today) —
  mounts auto-update, env never does.
- The backup CronJob and superuser secret are out of scope: the CNPG
  superuser (`postgres`) is a different credential, and with
  `enableSuperuserAccess: false` nothing consumes it.
