"""Business/private notification per trip (ADR 0003 in
containers/trip-enricher/docs/adr): every closed trip of every vehicle, any
driver, is pushed to the configured notification target(s) -- persons with
``"notify": true`` in TRIP_PHONES -- as an HA actionable notification with
two actions, TRIP_BIZ_<id> / TRIP_PRIV_<id>.

Runs inside the detector process:

* ``Notifier.run_once`` after every detector cycle sends at most
  TRIP_NOTIFY_MAX_PER_CYCLE notifications via HA REST
  ``POST /api/services/notify/<service>``.
* ``ActionListener`` holds an outbound HA websocket in a daemon thread
  (subscribe_events ``mobile_app_notification_action``) and writes the
  answer through ``TripStore.annotate(business=..., via="notify",
  actor="ha")``. Taps while the detector is down are lost; the trips page
  is the fallback.

``<id>`` is a short hash of the trip key (trip keys contain ':'), stored in
trips.notifications_sent.notify_id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from vehicle_pipeline.trips.store import EffectiveTrip

logger = logging.getLogger("vehicle_pipeline.trips.notify")

ACTION_BUSINESS = "TRIP_BIZ_"
ACTION_PRIVATE = "TRIP_PRIV_"
EVENT_TYPE = "mobile_app_notification_action"
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def notify_id(trip_key: str) -> str:
    """Short, action-ID-safe, deterministic handle for a trip key."""
    return hashlib.sha256(trip_key.encode()).hexdigest()[:12]


def parse_action(action: Any) -> tuple[bool, str] | None:
    """``TRIP_BIZ_<id>`` -> (True, id), ``TRIP_PRIV_<id>`` -> (False, id),
    anything else (other automations' actions) -> None."""
    if not isinstance(action, str):
        return None
    for prefix, business in ((ACTION_BUSINESS, True), (ACTION_PRIVATE, False)):
        if action.startswith(prefix):
            nid = action[len(prefix) :]
            return (business, nid) if _ID_RE.match(nid) else None
    return None


def eligible(
    candidates: Iterable[EffectiveTrip],
    *,
    now: datetime,
    cutoff: datetime,
    min_age: timedelta,
    limit: int,
    already_sent: Iterable[str] = (),
) -> list[EffectiveTrip]:
    """Trips to notify now, oldest first: closed for at least min_age,
    ended after the cutoff, not notified before, not already flagged
    business/private; at most ``limit``."""
    sent = set(already_sent)
    out = [
        t
        for t in candidates
        if t.ended_at is not None
        and t.ended_at > cutoff
        and t.ended_at <= now - min_age
        and t.business is None
        and t.trip_key not in sent
    ]
    out.sort(key=lambda t: (t.ended_at, t.trip_key))
    return out[: max(limit, 0)]


def _km_text(t: EffectiveTrip) -> str:
    if t.km is not None and t.status in ("ok", "odometer_split"):
        return f"{t.km:.1f} km"
    if t.gps_km is not None:
        return f"~{t.gps_km:.1f} km (GPS)"
    return "km offen"


def build_notification(
    t: EffectiveTrip,
    *,
    vehicle_names: Mapping[str, str],
    tz: ZoneInfo,
    public_base_url: str | None,
) -> dict[str, Any]:
    nid = notify_id(t.trip_key)
    local = t.started_at.astimezone(tz)
    vehicle = vehicle_names.get(t.vehicle_id or "", t.vehicle_id or "Fahrzeug ?")
    when = f"{_WEEKDAYS[local.weekday()]} {local:%d.%m. %H:%M}"
    message = " · ".join([vehicle, when, _km_text(t), t.driver or "Fahrer unbekannt"])
    data: dict[str, Any] = {
        "tag": f"trip_{nid}",
        "actions": [
            {"action": f"{ACTION_BUSINESS}{nid}", "title": "Business"},
            {"action": f"{ACTION_PRIVATE}{nid}", "title": "Private"},
        ],
    }
    if public_base_url:
        link = f"{public_base_url.rstrip('/')}/trips?trip={quote(t.trip_key, safe='')}"
        data["url"] = link  # iOS
        data["clickAction"] = link  # Android
    return {"title": "Fahrt: geschäftlich oder privat?", "message": message, "data": data}


class NotifyStore(Protocol):
    def ensure_notify_cutoff(self, now: datetime) -> datetime: ...
    def notify_candidates(self, cutoff: datetime, limit: int = 200) -> list[EffectiveTrip]: ...
    def mark_notified(self, trip_key: str, notify_id: str, target: str) -> None: ...


class ServiceCaller(Protocol):
    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None: ...


@dataclass
class Notifier:
    store: NotifyStore
    ha: ServiceCaller
    targets: Sequence[str]
    vehicle_names: Mapping[str, str]
    tz: ZoneInfo
    since: datetime | None = None
    max_per_cycle: int = 5
    min_age: timedelta = timedelta(minutes=10)
    public_base_url: str | None = None
    cutoff: datetime | None = None

    def start(self, now: datetime) -> datetime:
        """Fix the cutoff: TRIP_NOTIFY_SINCE if set, else the persisted
        first-start time (written now if this is the first start)."""
        persisted = self.store.ensure_notify_cutoff(now)
        self.cutoff = self.since or persisted
        logger.info("trip notifications enabled: %d target(s), cutoff %s", len(self.targets), self.cutoff.isoformat())
        return self.cutoff

    def run_once(self, now: datetime) -> dict[str, int]:
        if self.cutoff is None:
            self.start(now)
        assert self.cutoff is not None
        trips = eligible(
            self.store.notify_candidates(self.cutoff),
            now=now,
            cutoff=self.cutoff,
            min_age=self.min_age,
            limit=self.max_per_cycle,
        )
        stats = {"notify_sent": 0, "notify_failed": 0}
        for t in trips:
            payload = build_notification(
                t, vehicle_names=self.vehicle_names, tz=self.tz, public_base_url=self.public_base_url
            )
            delivered = []
            for target in self.targets:
                try:
                    self.ha.call_service("notify", target, payload)
                    delivered.append(target)
                except Exception as e:  # noqa: BLE001 - retried next cycle
                    logger.warning("notify %s for %s failed: %s", target, t.trip_key, e)
            if delivered:
                # Only marked once delivered, so a failed send is retried
                # next cycle; a crash between send and mark re-sends with
                # the same tag, which replaces the notification on the phone.
                self.store.mark_notified(t.trip_key, notify_id(t.trip_key), ",".join(delivered))
                stats["notify_sent"] += 1
            else:
                stats["notify_failed"] += 1
        return stats


# ----------------------------------------------------------- websocket side


class AnswerStore(Protocol):
    def trip_key_for_notify_id(self, notify_id: str) -> str | None: ...
    def annotate(self, trip_keys: str | Sequence[str], *, via: str, actor: str | None = None, **values: Any) -> int: ...


def ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + "/api/websocket"


def handle_event(msg: Mapping[str, Any], store: AnswerStore) -> str | None:
    """One websocket message -> annotation. Returns the annotated trip key,
    or None if the message is not one of our notification actions."""
    if msg.get("type") != "event":
        return None
    event = msg.get("event") or {}
    if event.get("event_type") != EVENT_TYPE:
        return None
    parsed = parse_action((event.get("data") or {}).get("action"))
    if parsed is None:
        return None
    business, nid = parsed
    trip_key = store.trip_key_for_notify_id(nid)
    if trip_key is None:
        logger.warning("notification action for unknown notify id %s", nid)
        return None
    store.annotate(trip_key, business=business, via="notify", actor="ha")
    logger.info("trip %s flagged %s via notification", trip_key, "business" if business else "private")
    return trip_key


class WsConn(Protocol):
    def send(self, message: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...


class AuthError(RuntimeError):
    pass


def run_session(ws: WsConn, token: str, store: AnswerStore, stop: threading.Event, recv_timeout: float = 5.0) -> None:
    """Authenticate, subscribe, then dispatch events until ``stop`` is set
    or the connection fails (raises)."""

    def recv() -> dict[str, Any]:
        return json.loads(ws.recv(timeout=30))

    first = recv()
    if first.get("type") != "auth_required":
        raise RuntimeError(f"unexpected first message {first.get('type')!r}")
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth = recv()
    if auth.get("type") != "auth_ok":
        raise AuthError(f"HA websocket auth failed: {auth.get('type')}")
    ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": EVENT_TYPE}))
    ack = recv()
    if ack.get("type") != "result" or not ack.get("success"):
        raise RuntimeError("subscribe_events failed")
    logger.info("subscribed to %s", EVENT_TYPE)
    while not stop.is_set():
        try:
            raw = ws.recv(timeout=recv_timeout)
        except TimeoutError:
            continue
        try:
            handle_event(json.loads(raw), store)
        except Exception:
            # A bad message or a DB hiccup must not drop the subscription.
            logger.exception("handling notification action failed")


class ActionListener:
    """Daemon thread holding the HA websocket, reconnecting with
    exponential backoff (1 s .. 5 min). Independent of the detector loop,
    so it can never starve the heartbeat."""

    def __init__(
        self,
        ha_url: str,
        token: str,
        store: AnswerStore,
        connect: Callable[[str], Any] | None = None,
        max_backoff: float = 300.0,
    ) -> None:
        self._url = ws_url(ha_url)
        self._token = token
        self._store = store
        self._connect = connect or _default_connect
        self._max_backoff = max_backoff
        self.stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ha-notify-actions", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        initial = min(1.0, self._max_backoff)
        backoff = initial
        while not self.stop_event.is_set():
            try:
                with self._connect(self._url) as ws:
                    backoff = initial
                    run_session(ws, self._token, self._store, self.stop_event)
            except AuthError as e:
                logger.error("%s; retrying in %.0fs", e, self._max_backoff)
                backoff = self._max_backoff
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                logger.warning("HA websocket dropped (%s); reconnecting in %.0fs", e, backoff)
            if self.stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, self._max_backoff)


def _default_connect(url: str) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=30, max_size=4 * 1024 * 1024)
