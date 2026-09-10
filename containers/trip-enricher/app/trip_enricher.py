#!/usr/bin/env python3
"""trip-enricher: enrich MyGarage drive sessions with driver attribution.

Polls MyGarage for newly closed LiveLink sessions, attributes a driver
using Home Assistant phone sensor history (hotspot SSID / CarPlay /
Automotive activity), stores tax-relevant trip records plus GPS
breadcrumbs in Postgres, and leaves business/private classification
for later.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests

HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/trip-enricher-heartbeat")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
HA_WINDOW_PAD = timedelta(minutes=15)

REQUIRED_ENV = [
    "MYGARAGE_BASE_URL",
    "MYGARAGE_USERNAME",
    "MYGARAGE_PASSWORD",
    "HA_URL",
    "HA_TOKEN",
    "DATABASE_URL",
    "VEHICLES_JSON",
    "PHONES_JSON",
]


def log(level, msg, **kv):
    parts = [f"ts={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"level={level}", f"msg={json.dumps(msg)}"]
    for k, v in kv.items():
        parts.append(f"{k}={json.dumps(v)}")
    print(" ".join(parts), flush=True)


def touch_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        log("warn", "heartbeat write failed", error=str(e))


def with_retry(fn, desc, attempts=4, base_delay=2.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            delay = base_delay * (2 ** i)
            log("warn", "retrying after error", op=desc, attempt=i + 1,
                attempts=attempts, delay_s=delay, error=str(e))
            time.sleep(delay)
    raise last


def pick(d, *keys):
    """Return the first present non-null key from a dict (field-name variance)."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch seconds or milliseconds
        if value > 1e12:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_ts(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            # MyGarage serializes UTC timestamps without a tz offset; a naive
            # ISO string in the HA history path is interpreted as HA-server
            # local time, silently shifting the query window. Assume UTC.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            log("warn", "unparseable timestamp", value=value)
            return None
    return None


def parse_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_vehicles(raw):
    """VEHICLES_JSON -> list of {vin, name, hotspot_ssid}.

    Accepts a list of objects or a dict keyed by VIN.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"VEHICLES_JSON is not valid JSON: {e}")
    if isinstance(data, dict):
        entries = list(data.items())
    elif isinstance(data, list):
        entries = [(None, item) for item in data]
    else:
        entries = []
    vehicles = []
    for key, item in entries:
        if not isinstance(item, dict):
            log("warn", "skipping malformed vehicle entry", entry=str(item)[:120])
            continue
        vin = pick(item, "vin", "VIN") or key
        if not vin:
            log("warn", "vehicle entry without vin", entry=str(item)[:120])
            continue
        vehicles.append({
            "vin": str(vin),
            "name": str(pick(item, "name", "vehicle", "label") or vin),
            "hotspot_ssid": pick(item, "hotspot_ssid", "hotspotSsid", "ssid"),
        })
    return vehicles


def normalize_phones(raw):
    """PHONES_JSON -> {person: {tracker, ssid, audio, activity}}.

    Accepts a dict keyed by person name or a list of {name, ...} objects.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PHONES_JSON is not valid JSON: {e}")
    phones = {}
    if isinstance(data, dict):
        entries = [(name, cfg) for name, cfg in data.items()]
    elif isinstance(data, list):
        entries = [(pick(c, "name", "person"), c) for c in data if isinstance(c, dict)]
    else:
        entries = []
    for name, cfg in entries:
        if not name or not isinstance(cfg, dict):
            log("warn", "skipping malformed phone entry", entry=str(cfg)[:120])
            continue
        phones[str(name)] = {
            "tracker": pick(cfg, "tracker", "device_tracker"),
            "ssid": pick(cfg, "ssid", "ssid_sensor"),
            "audio": pick(cfg, "audio", "audio_sensor"),
            "activity": pick(cfg, "activity", "activity_sensor"),
        }
    return phones


class MyGarageClient:
    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.csrf_token = None

    class AuthError(Exception):
        pass

    def login(self):
        def call():
            r = self.session.post(
                f"{self.base}/api/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=30,
            )
            r.raise_for_status()
            return r
        r = with_retry(call, "mygarage login")
        token = None
        try:
            body = r.json()
            token = pick(body, "csrf_token", "csrfToken", "csrf", "token",
                         "xsrf_token", "xsrfToken")
        except ValueError:
            pass
        token = token or r.headers.get("X-CSRF-Token") or r.headers.get("X-XSRF-TOKEN")
        if token:
            self.csrf_token = str(token)
            log("info", "mygarage login ok", csrf="captured")
        else:
            self.csrf_token = None
            log("info", "mygarage login ok", csrf="absent")

    def _headers(self):
        h = {}
        if self.csrf_token:
            h["X-CSRF-Token"] = self.csrf_token
        return h

    def get_sessions(self, vin):
        def call():
            r = self.session.get(
                f"{self.base}/api/vehicles/{vin}/livelink/sessions",
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code in (401, 403):
                raise self.AuthError(f"status={r.status_code}")
            r.raise_for_status()
            return r.json()
        try:
            return with_retry(call, f"mygarage sessions vin={vin}")
        except self.AuthError:
            log("info", "mygarage auth expired, re-login", vin=vin)
            self.login()
            return with_retry(call, f"mygarage sessions vin={vin} (after relogin)")


def extract_sessions(payload, vin):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("sessions", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        log("warn", "unknown sessions payload shape", vin=vin,
            keys=sorted(payload.keys()))
    else:
        log("warn", "unknown sessions payload type", vin=vin,
            type=type(payload).__name__)
    return []


def parse_session(raw, vin):
    """Normalize a raw session dict; return None if unusable."""
    if not isinstance(raw, dict):
        log("warn", "session entry is not an object", vin=vin, entry=str(raw)[:120])
        return None
    session_id = pick(raw, "id", "session_id", "sessionId", "uuid")
    if session_id is None:
        log("warn", "session without id, skipping", vin=vin,
            keys=sorted(raw.keys()))
        return None
    started = parse_ts(pick(raw, "start_time", "startTime", "start",
                            "started_at", "startedAt", "begin"))
    ended = parse_ts(pick(raw, "end_time", "endTime", "end",
                          "ended_at", "endedAt", "finish"))
    distance = parse_number(pick(raw, "distance_km", "distanceKm",
                                 "distance", "distance_kilometers"))
    odo_start = parse_number(pick(raw, "odometer_start", "odometerStart",
                                  "start_odometer", "odo_start"))
    odo_end = parse_number(pick(raw, "odometer_end", "odometerEnd",
                                "end_odometer", "odo_end"))
    return {
        "session_id": str(session_id),
        "started_at": started,
        "ended_at": ended,
        "distance_km": distance,
        "odometer_start": odo_start,
        "odometer_end": odo_end,
    }


def fetch_ha_history(ha_url, ha_token, entity_ids, start, end):
    entity_ids = [e for e in entity_ids if e]
    if not entity_ids:
        return {}
    url = (f"{ha_url.rstrip('/')}/api/history/period/"
           f"{start.isoformat()}")
    params = {
        "filter_entity_id": ",".join(entity_ids),
        "end_time": end.isoformat(),
    }
    headers = {"Authorization": f"Bearer {ha_token}"}

    def call():
        r = requests.get(url, params=params, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()
    payload = with_retry(call, "ha history")
    states = {}
    if isinstance(payload, list):
        for entity_states in payload:
            if isinstance(entity_states, list) and entity_states:
                eid = entity_states[0].get("entity_id")
                if eid:
                    states[eid] = [s for s in entity_states if isinstance(s, dict)]
    else:
        log("warn", "unknown HA history payload type", type=type(payload).__name__)
    return states


def _state_value(entry):
    v = entry.get("state")
    return None if v in (None, "unknown", "unavailable") else str(v)


def score_person(cfg, history, hotspot_ssid):
    score = 0
    signals = []
    if hotspot_ssid:
        for e in history.get(cfg.get("ssid") or "", []):
            if _state_value(e) == str(hotspot_ssid):
                score += 10
                signals.append("hotspot_ssid")
                break
    for e in history.get(cfg.get("audio") or "", []):
        if _state_value(e) == "CarPlay":
            score += 5
            signals.append("carplay")
            break
    for e in history.get(cfg.get("activity") or "", []):
        if _state_value(e) == "Automotive":
            score += 3
            signals.append("automotive")
            break
    return score, signals


def haversine_km(points):
    """Sum of great-circle distances between consecutive points."""
    from math import asin, cos, radians, sin, sqrt
    total = 0.0
    for a, b in zip(points, points[1:]):
        dlat = radians(b["lat"] - a["lat"])
        dlon = radians(b["lon"] - a["lon"])
        h = (sin(dlat / 2) ** 2
             + cos(radians(a["lat"])) * cos(radians(b["lat"]))
             * sin(dlon / 2) ** 2)
        total += 6371.0 * 2 * asin(sqrt(h))
    return total


def breadcrumbs_from_tracker(entries):
    points = []
    for e in entries:
        attrs = e.get("attributes") or {}
        lat = parse_number(attrs.get("latitude"))
        lon = parse_number(attrs.get("longitude"))
        if lat is None or lon is None:
            continue
        ts = parse_ts(e.get("last_changed") or e.get("last_updated"))
        if ts is None:
            continue
        speed = parse_number(attrs.get("speed"))
        points.append({"ts": ts, "lat": lat, "lon": lon, "speed_kmh": speed})
    return points


SCHEMA_SQL = [
    "CREATE SCHEMA IF NOT EXISTS trips",
    """
    CREATE TABLE IF NOT EXISTS trips.trip_records (
        id serial PRIMARY KEY,
        session_id text UNIQUE NOT NULL,
        vin text NOT NULL,
        vehicle text,
        driver text,
        started_at timestamptz,
        ended_at timestamptz,
        distance_km numeric,
        odometer_start numeric,
        odometer_end numeric,
        business boolean,
        purpose text,
        evidence jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trips.trip_positions (
        id serial PRIMARY KEY,
        session_id text NOT NULL,
        ts timestamptz NOT NULL,
        lat double precision,
        lon double precision,
        speed_kmh numeric,
        UNIQUE (session_id, ts)
    )
    """,
    "ALTER TABLE trips.trip_records ADD COLUMN IF NOT EXISTS distance_km_effective numeric",
    "ALTER TABLE trips.trip_records ADD COLUMN IF NOT EXISTS overlap_note text",
]


def ensure_schema(conn):
    with conn.cursor() as cur:
        for stmt in SCHEMA_SQL:
            cur.execute(stmt)


def trip_exists(conn, session_id):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM trips.trip_records WHERE session_id = %s",
                    (session_id,))
        return cur.fetchone() is not None


def compute_effective_distance(conn, vin, session):
    """Subtract odometer intervals already claimed by earlier trips.

    MyGarage backfill can attribute an entire missing odometer range to a
    zero-length phantom session, double-counting trips already recorded.
    The effective distance is the session's reported distance scaled by the
    fraction of its odometer span not covered by earlier records. Returns
    (effective_km, note|None).
    """
    odo_start = session["odometer_start"]
    odo_end = session["odometer_end"]
    distance = session["distance_km"]
    if odo_start is None or odo_end is None or distance is None:
        return distance, None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT odometer_start, odometer_end FROM trips.trip_records
            WHERE vin = %s AND odometer_start IS NOT NULL
              AND odometer_end IS NOT NULL
              AND odometer_start < %s AND odometer_end > %s
            """,
            (vin, odo_end, odo_start),
        )
        overlaps = cur.fetchall()
    if not overlaps:
        return distance, None
    intervals = sorted(
        (max(float(s), float(odo_start)), min(float(e), float(odo_end)))
        for s, e in overlaps
    )
    merged = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    overlap_km = sum(e - s for s, e in merged)
    span = float(odo_end) - float(odo_start)
    effective = (round(float(distance) * max(0.0, span - overlap_km) / span, 1)
                 if span > 0 else 0.0)
    note = (f"odometer overlap {overlap_km:.1f}km with earlier sessions; "
            f"distance scaled to uncovered span")
    return effective, note


def insert_trip(conn, session, vehicle, driver, evidence):
    if session["distance_km"] is None:
        # No vehicle odometer at all (e.g. T7 gateway diagnostic filter):
        # distance comes from the attributed phone's GPS breadcrumbs.
        effective = session.get("gps_distance_km")
        note = "distance derived from attributed phone GPS (no vehicle odometer)"
    else:
        effective, note = compute_effective_distance(conn, vehicle["vin"], session)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trips.trip_records
                (session_id, vin, vehicle, driver, started_at, ended_at,
                 distance_km, distance_km_effective, odometer_start,
                 odometer_end, overlap_note, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session["session_id"], vehicle["vin"], vehicle["name"], driver,
             session["started_at"], session["ended_at"], session["distance_km"],
             effective, session["odometer_start"], session["odometer_end"],
             note, json.dumps(evidence)),
        )
    return effective, note


def insert_positions(conn, session_id, points):
    with conn.cursor() as cur:
        for p in points:
            cur.execute(
                """
                INSERT INTO trips.trip_positions
                    (session_id, ts, lat, lon, speed_kmh)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (session_id, ts) DO NOTHING
                """,
                (session_id, p["ts"], p["lat"], p["lon"], p["speed_kmh"]),
            )


def push_positions_to_mygarage(conn, vin, session_id, points):
    """Write GPS breadcrumbs into MyGarage's own location_points table so its
    Trips view (sessions with >=1 location point) picks them up. Mirrors
    LocationService.record_point: naive-UTC timestamps, dedup on
    (vin, timestamp, source). Returns rows inserted."""
    inserted = 0
    with conn.cursor() as cur:
        for p in points:
            cur.execute(
                """
                INSERT INTO location_points
                    (vin, drive_session_id, source, timestamp,
                     latitude, longitude, speed)
                VALUES (%s, %s, 'enricher', %s, %s, %s, %s)
                ON CONFLICT (vin, timestamp, source) DO NOTHING
                """,
                (vin, int(session_id), p["ts"].replace(tzinfo=None),
                 p["lat"], p["lon"], p["speed_kmh"]),
            )
            inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return inserted


def process_session(conn, mg, session, vehicle, phones, ha_url, ha_token):
    start = session["started_at"]
    end = session["ended_at"]
    if start is None:
        start = end  # degenerate; still score with padded window around end
    entity_ids = []
    for cfg in phones.values():
        entity_ids.extend([cfg["tracker"], cfg["ssid"], cfg["audio"],
                           cfg["activity"]])
    history = fetch_ha_history(ha_url, ha_token, entity_ids,
                               start - HA_WINDOW_PAD, end + HA_WINDOW_PAD)

    scores = {}
    signals = {}
    for person, cfg in phones.items():
        s, sig = score_person(cfg, history, vehicle.get("hotspot_ssid"))
        scores[person] = s
        signals[person] = sig
    if scores and max(scores.values()) > 0:
        driver = max(scores, key=scores.get)
    else:
        driver = "unknown"

    evidence = {
        "scores": scores,
        "signals": signals,
        "hotspot_ssid": vehicle.get("hotspot_ssid"),
        "window": {"start": (start - HA_WINDOW_PAD).isoformat(),
                   "end": (end + HA_WINDOW_PAD).isoformat()},
    }

    points = []
    if driver != "unknown":
        tracker = phones.get(driver, {}).get("tracker")
        if tracker:
            points = breadcrumbs_from_tracker(history.get(tracker, []))

    if not session["distance_km"] or session["distance_km"] <= 0:
        # Session without usable vehicle distance: only keep it if the
        # phone GPS shows a genuine drive, otherwise it's a stationary
        # wake/phantom session.
        duration_s = ((end - start).total_seconds() if start and end else 0)
        gps_km = round(haversine_km(points), 1) if len(points) >= 2 else 0.0
        if duration_s < 60 or gps_km < 0.5:
            log("info", "session skipped (no movement)",
                session_id=session["session_id"], vin=vehicle["vin"],
                distance_km=session["distance_km"],
                duration_s=duration_s, gps_km=gps_km)
            return
        session["distance_km"] = None
        session["gps_distance_km"] = gps_km

    effective, note = insert_trip(conn, session, vehicle, driver, evidence)
    insert_positions(conn, session["session_id"], points)
    mg_points = 0
    if points:
        try:
            mg_points = push_positions_to_mygarage(
                conn, vehicle["vin"], session["session_id"], points)
        except Exception as e:
            log("error", "mygarage location_points push failed",
                session_id=session["session_id"], error=str(e))
    log("info", "session processed", session_id=session["session_id"],
        vin=vehicle["vin"], vehicle=vehicle["name"], driver=driver,
        distance_km=session["distance_km"], distance_km_effective=effective,
        overlap_note=note, positions=len(points), mg_location_points=mg_points)


def connect_db(dsn):
    # DATABASE_URL uses the SQLAlchemy-style postgresql+psycopg:// scheme;
    # psycopg.connect wants a plain postgresql:// URI.
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return with_retry(lambda: psycopg.connect(dsn, autocommit=True),
                      "postgres connect")


def run_loop():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log("error", "missing required env vars", missing=missing)
        sys.exit(1)

    base_url = os.environ["MYGARAGE_BASE_URL"]
    mg = MyGarageClient(base_url, os.environ["MYGARAGE_USERNAME"],
                        os.environ["MYGARAGE_PASSWORD"])
    ha_url = os.environ["HA_URL"]
    ha_token = os.environ["HA_TOKEN"]
    vehicles = normalize_vehicles(os.environ["VEHICLES_JSON"])
    phones = normalize_phones(os.environ["PHONES_JSON"])
    if not vehicles:
        log("error", "no vehicles configured")
        sys.exit(1)
    log("info", "starting", vehicles=len(vehicles), persons=len(phones),
        poll_interval_s=POLL_INTERVAL)

    conn = connect_db(os.environ["DATABASE_URL"])
    ensure_schema(conn)
    mg.login()

    while True:
        touch_heartbeat()
        try:
            if conn.closed:
                conn = connect_db(os.environ["DATABASE_URL"])
                ensure_schema(conn)
            for vehicle in vehicles:
                try:
                    payload = mg.get_sessions(vehicle["vin"])
                except Exception as e:
                    log("error", "failed to fetch sessions", vin=vehicle["vin"],
                        error=str(e))
                    continue
                # Odometer-overlap dedup assumes earlier trips are recorded
                # first — process in chronological order, not API order.
                parsed = []
                for raw in extract_sessions(payload, vehicle["vin"]):
                    session = parse_session(raw, vehicle["vin"])
                    if session is None:
                        continue
                    if session["ended_at"] is None:
                        continue  # still open
                    # distance-less sessions are handled in process_session
                    # (phone-GPS fallback vs. phantom-skip)
                    parsed.append(session)
                parsed.sort(key=lambda s: s["started_at"] or s["ended_at"])
                for session in parsed:
                    try:
                        if trip_exists(conn, session["session_id"]):
                            continue
                        process_session(conn, mg, session, vehicle, phones,
                                        ha_url, ha_token)
                    except Exception as e:
                        log("error", "session processing failed",
                            session_id=session["session_id"], error=str(e))
        except Exception as e:
            log("error", "loop iteration failed", error=str(e))
        touch_heartbeat()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_loop()
