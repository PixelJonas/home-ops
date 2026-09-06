#!/usr/bin/env bash
# Idempotent MyGarage bootstrap: app-level config that cannot live in
# manifests (admin account, vehicles, LiveLink MQTT, Authelia OIDC wiring,
# WiCAN device link + SD backfill). All values come from Doppler
# (homelab/home) — nothing site-specific is hardcoded here.
#
# Vehicles/devices are defined by the MYGARAGE_VEHICLES Doppler key (JSON
# array of {vin, nickname, year, make, model, device_id, device_label,
# sd_backfill}). If the key is absent the script falls back to the legacy
# single-vehicle path (MYGARAGE_VEHICLE_VIN + hardcoded Multivan/WiCAN
# values), so it stays safe to run before the key is created.
#
# Usage: ./bootstrap.sh [base-url]   (default: https://mygarage.apps.altus.janz.digital)
set -euo pipefail

BASE_URL="${1:-https://mygarage.apps.altus.janz.digital}"
export BASE_URL

# Doppler access (homelab/home via the bridge token in infra-ops/prd)
source /home/jonas/projects/infra-ops/.env.doppler
HB=$(doppler secrets get ASGARD_DOPPLER_HOMELAB_TOKEN --plain -p infra-ops -c prd -t "$DOPPLER_TOKEN")
export HB

python3 - <<'PYEOF'
import json, os, subprocess, sys, urllib.request, http.cookiejar

BASE = os.environ["BASE_URL"]

def doppler(key):
    return subprocess.run(
        ["doppler", "secrets", "get", key, "--plain", "-p", "homelab", "-c", "home",
         "-t", os.environ["HB"]], capture_output=True, text=True, check=True).stdout.strip()

def doppler_opt(key):
    p = subprocess.run(
        ["doppler", "secrets", "get", key, "--plain", "-p", "homelab", "-c", "home",
         "-t", os.environ["HB"]], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None

cj = http.cookiejar.MozillaCookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def call(method, path, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        r = op.open(req, timeout=20)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw[:200]}

admin_user = doppler("MYGARAGE_ADMIN_USERNAME")
admin_pw = doppler("MYGARAGE_ADMIN_PASSWORD")
mqtt_user = doppler("MYGARAGE_MQTT_USERNAME")
mqtt_pw = doppler("MYGARAGE_MQTT_PASSWORD")
oidc_secret = doppler("MYGARAGE_OIDC_CLIENT_SECRET")

# Vehicle definitions: MYGARAGE_VEHICLES (JSON array) if present, else the
# legacy single-vehicle path (MYGARAGE_VEHICLE_VIN + hardcoded values).
vehicles_json = doppler_opt("MYGARAGE_VEHICLES")
if vehicles_json:
    vehicles_cfg = json.loads(vehicles_json)
    assert isinstance(vehicles_cfg, list), "MYGARAGE_VEHICLES must be a JSON array"
    print(f"multi-vehicle config: {len(vehicles_cfg)} vehicle(s) from MYGARAGE_VEHICLES")
else:
    vehicles_cfg = [{
        "vin": doppler("MYGARAGE_VEHICLE_VIN"),
        "nickname": "Multivan T7", "year": 2026,
        "make": "Volkswagen", "model": "Multivan",
        "device_id": "206ef18af0b5", "device_label": "WiCAN Multivan",
        "sd_backfill": True}]
    print("legacy single-vehicle config (MYGARAGE_VEHICLES not set)")

# 1. admin account (first user becomes admin; registration closes after)
_, r = call("GET", "/api/auth/users/count")
if r.get("count", 0) == 0:
    st, r = call("POST", "/api/auth/register", {
        "username": admin_user, "email": "jonas@janz.digital",
        "password": admin_pw, "full_name": "Jonas"})
    assert st in (200, 201), f"register failed: {st} {r}"
    print("admin registered")
else:
    print("admin exists, skipping registration")

st, r = call("POST", "/api/auth/login", {"username": admin_user, "password": admin_pw})
assert st == 200, f"login failed: {st} {r}"
H = {"X-CSRF-Token": r.get("csrf_token", "")}

# 2. vehicles
st, r = call("GET", "/api/vehicles")
existing = r if isinstance(r, list) else r.get("vehicles", [])
for v in vehicles_cfg:
    if not any(e.get("vin") == v["vin"] for e in existing):
        st, r = call("POST", "/api/vehicles", {
            "nickname": v["nickname"], "vehicle_type": "Car", "vin": v["vin"],
            "year": v["year"], "make": v["make"], "model": v["model"]}, H)
        assert st in (200, 201), f"vehicle create failed: {st} {r}"
        print(f"vehicle '{v['nickname']}' created")
    else:
        print(f"vehicle '{v['nickname']}' exists")

# 3. LiveLink MQTT subscription
st, r = call("PUT", "/api/livelink/mqtt/settings", {
    "enabled": True, "broker_host": "10.42.0.12", "broker_port": 1883,
    "username": mqtt_user, "password": mqtt_pw,
    "topic_prefix": "wican", "use_tls": False}, H)
assert st == 200, f"mqtt settings failed: {st} {r}"
call("POST", "/api/livelink/mqtt/restart", {}, H)
print("mqtt configured")

# 4. Authelia OIDC
st, r = call("PUT", "/api/auth/oidc/config/admin", {
    "enabled": True, "provider_name": "Authelia",
    "issuer_url": "https://auth.app.janz.digital",
    "client_id": "mygarage", "client_secret": oidc_secret,
    "scopes": "openid profile email groups",
    "auto_create_users": True, "admin_group": "admins",
    "username_claim": "preferred_username", "email_claim": "email",
    "full_name_claim": "name"}, H)
assert st == 200, f"oidc config failed: {st} {r}"
print("oidc configured")

# 5. WiCAN device link + SD backfill (per vehicle with a device_id)
st, r = call("GET", "/api/livelink/devices")
devs = r.get("devices", [])
for v in vehicles_cfg:
    device = v.get("device_id", "")
    if not device:
        print(f"NOTE: '{v['nickname']}' has no device_id — vehicle created, device link pending")
        continue
    if any(d.get("device_id") == device for d in devs):
        st, r = call("PUT", f"/api/livelink/devices/{device}",
                     {"vin": v["vin"],
                      "label": v.get("device_label", f"WiCAN {v['nickname']}")}, H)
        assert st == 200, f"device link failed: {st} {r}"
        backfill = bool(v.get("sd_backfill", True))
        st, r = call("PUT", f"/api/livelink/devices/{device}/sd-config",
                     {"sd_backfill_enabled": backfill}, H)
        assert st == 200, f"sd-config failed: {st} {r}"
        print(f"device {device} linked to '{v['nickname']}', "
              f"sd backfill {'enabled' if backfill else 'disabled'}")
    else:
        print(f"WARN: WiCAN device {device} not discovered yet "
              "(appears once it publishes); rerun then")

print("bootstrap complete")
PYEOF
