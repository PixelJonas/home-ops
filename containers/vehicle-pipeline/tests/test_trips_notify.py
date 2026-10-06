"""Business/private notifications: eligibility, action IDs, payload, the
Notifier send loop and the websocket answer path (fake HA, fake ws)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from vehicle_pipeline.trips.notify import (
    ActionListener,
    AuthError,
    Notifier,
    build_notification,
    eligible,
    handle_event,
    notify_id,
    parse_action,
    run_session,
    ws_url,
)
from vehicle_pipeline.trips.settings import TripSettings, parse_phones
from vehicle_pipeline.trips.store import EffectiveTrip

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Berlin")


def trip(
    key: str,
    ended_min_ago: float | None,
    *,
    business: bool | None = None,
    km: str | None = "12.3",
    gps_km: str | None = "12.0",
    status: str = "ok",
    driver: str | None = "alice",
) -> EffectiveTrip:
    ended = None if ended_min_ago is None else NOW - timedelta(minutes=ended_min_ago)
    started = (ended or NOW) - timedelta(minutes=30)
    return EffectiveTrip(
        trip_key=key,
        vehicle_id="id4",
        driver=driver,
        started_at=started,
        ended_at=ended,
        km=Decimal(km) if km else None,
        gps_km=Decimal(gps_km) if gps_km else None,
        status=status,
        source="ha_phone",
        business=business,
        business_via=None,
        purpose=None,
        detected_vehicle_id="id4",
        detected_driver=driver,
    )


def _eligible(trips: list[EffectiveTrip], limit: int = 5, sent: tuple[str, ...] = ()) -> list[str]:
    return [
        t.trip_key
        for t in eligible(trips, now=NOW, cutoff=CUTOFF, min_age=timedelta(minutes=10), limit=limit, already_sent=sent)
    ]


# ------------------------------------------------------------ eligibility


def test_eligible_requires_closed_trip_older_than_min_age() -> None:
    assert _eligible([trip("span:1", None), trip("span:2", 5), trip("span:3", 10), trip("span:4", 60)]) == [
        "span:4",
        "span:3",
    ]


def test_eligible_respects_cutoff() -> None:
    # Ended 5 h ago = before the 08:00 cutoff (backfilled history).
    assert _eligible([trip("span:old", 5 * 60), trip("span:new", 60)]) == ["span:new"]


def test_eligible_skips_already_sent_and_already_flagged() -> None:
    trips = [trip("span:1", 60), trip("span:2", 60, business=False), trip("span:3", 60, business=True), trip("span:4", 60)]
    assert _eligible(trips, sent=("span:1",)) == ["span:4"]


def test_eligible_rate_limit_takes_oldest_first() -> None:
    trips = [trip(f"span:{i}", 20 + i) for i in range(8)]
    assert _eligible(trips, limit=3) == ["span:7", "span:6", "span:5"]
    assert _eligible(trips, limit=0) == []


# -------------------------------------------------------------- action ids


def test_notify_id_is_short_safe_and_stable() -> None:
    nid = notify_id("gap:id4:1234")
    assert nid == notify_id("gap:id4:1234")
    assert len(nid) == 12 and nid.isalnum()
    assert nid != notify_id("gap:id4:1235")


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("TRIP_BIZ_0123456789ab", (True, "0123456789ab")),
        ("TRIP_PRIV_0123456789ab", (False, "0123456789ab")),
        ("TRIP_BIZ_span:12", None),
        ("TRIP_BIZ_", None),
        ("TRIP_PRIV_0123456789abcd", None),
        ("KWL_HEAT_OFF", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_action(action: Any, expected: Any) -> None:
    assert parse_action(action) == expected


def test_actions_round_trip_through_parse() -> None:
    payload = build_notification(trip("span:7", 60), vehicle_names={}, tz=TZ, public_base_url=None)
    parsed = [parse_action(a["action"]) for a in payload["data"]["actions"]]
    assert parsed == [(True, notify_id("span:7")), (False, notify_id("span:7"))]


# ----------------------------------------------------------------- payload


def test_payload_message_tag_and_no_url_without_base() -> None:
    p = build_notification(trip("span:7", 60), vehicle_names={"id4": "Test ID.4"}, tz=TZ, public_base_url=None)
    assert p["data"]["tag"] == f"trip_{notify_id('span:7')}"
    assert [a["title"] for a in p["data"]["actions"]] == ["Business", "Private"]
    assert "url" not in p["data"] and "clickAction" not in p["data"]
    # started 10:30 UTC = 12:30 Berlin (CEST), a Tuesday.
    assert p["message"] == "Test ID.4 · Di 06.10. 12:30 · 12.3 km · alice"


def test_payload_falls_back_to_gps_km_and_links_trip() -> None:
    t = trip("span:7", 60, km=None, status="odometer_pending", driver=None)
    p = build_notification(t, vehicle_names={}, tz=TZ, public_base_url="https://example.test/")
    assert "~12.0 km (GPS)" in p["message"] and "Fahrer unbekannt" in p["message"]
    assert p["data"]["url"] == "https://example.test/trips?trip=span%3A7"
    assert p["data"]["clickAction"] == p["data"]["url"]


# ---------------------------------------------------------------- notifier


class FakeStore:
    def __init__(self, trips: list[EffectiveTrip], persisted_cutoff: datetime = CUTOFF) -> None:
        self.trips = trips
        self.persisted = persisted_cutoff
        self.sent: dict[str, tuple[str, str]] = {}
        self.annotations: list[tuple[str, dict[str, Any], str, str | None]] = []

    def ensure_notify_cutoff(self, now: datetime) -> datetime:
        return self.persisted

    def notify_candidates(self, cutoff: datetime, limit: int = 200) -> list[EffectiveTrip]:
        return [t for t in self.trips if t.trip_key not in self.sent]

    def mark_notified(self, trip_key: str, nid: str, target: str) -> None:
        self.sent[trip_key] = (nid, target)

    def trip_key_for_notify_id(self, nid: str) -> str | None:
        return next((k for k, (n, _) in self.sent.items() if n == nid), None)

    def annotate(self, trip_keys: Any, *, via: str, actor: str | None = None, **values: Any) -> int:
        self.annotations.append((trip_keys, values, via, actor))
        return 1


class FakeHA:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail = fail or set()

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        if service in self.fail:
            raise RuntimeError("HA down")
        self.calls.append((domain, service, data))


def _notifier(store: FakeStore, ha: FakeHA, **kw: Any) -> Notifier:
    return Notifier(store=store, ha=ha, targets=["mobile_app_test_phone"], vehicle_names={}, tz=TZ, **kw)


def test_notifier_sends_marks_and_rate_limits() -> None:
    store = FakeStore([trip(f"span:{i}", 30 + i) for i in range(7)])
    ha = FakeHA()
    n = _notifier(store, ha, max_per_cycle=5)
    assert n.run_once(NOW) == {"notify_sent": 5, "notify_failed": 0}
    assert {c[:2] for c in ha.calls} == {("notify", "mobile_app_test_phone")}
    assert len(store.sent) == 5
    assert n.run_once(NOW)["notify_sent"] == 2
    assert n.run_once(NOW)["notify_sent"] == 0


def test_notifier_does_not_mark_failed_sends() -> None:
    store = FakeStore([trip("span:1", 30)])
    n = _notifier(store, FakeHA(fail={"mobile_app_test_phone"}))
    assert n.run_once(NOW) == {"notify_sent": 0, "notify_failed": 1}
    assert store.sent == {}


def test_notifier_cutoff_prefers_env_since_over_persisted() -> None:
    since = NOW - timedelta(days=30)
    store = FakeStore([trip("span:old", 5 * 60)])
    n = _notifier(store, FakeHA(), since=since)
    assert n.start(NOW) == since
    assert n.run_once(NOW)["notify_sent"] == 1
    # Without TRIP_NOTIFY_SINCE the persisted first-start cutoff applies.
    store2 = FakeStore([trip("span:old", 5 * 60)])
    assert _notifier(store2, FakeHA()).run_once(NOW)["notify_sent"] == 0


# --------------------------------------------------------------- settings


def test_phone_notify_flag_and_target_derivation() -> None:
    phones = parse_phones(
        json.dumps(
            {
                "alice": {"tracker": "device_tracker.alice_phone", "notify": True},
                "bob": {"tracker": "device_tracker.bob_phone"},
                "carol": {"tracker": "device_tracker.carol_phone", "notify": "true", "notify_service": "notify.carol_tablet"},
                "dave": {"notify": True},
            }
        )
    )
    targets = {p.person: p.notify_target() for p in phones}
    assert targets == {"alice": "mobile_app_alice_phone", "bob": None, "carol": "carol_tablet", "dave": None}


def test_notify_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("HA_URL", "http://ha.test:8123/")
    monkeypatch.setenv("HA_TOKEN", "t")
    monkeypatch.setenv("TRIP_VEHICLES", json.dumps({"VINFAKE00000000A1": {"name": "Our ID.4", "slug": "id4"}}))
    monkeypatch.setenv("TRIP_PHONES", json.dumps({"alice": {"tracker": "device_tracker.alice_phone", "notify": True}}))
    s = TripSettings.from_env()
    assert s.notify_enabled is False and s.public_base_url is None and s.notify_since is None
    monkeypatch.setenv("TRIP_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("TRIP_NOTIFY_SINCE", "2026-10-01T00:00:00Z")
    monkeypatch.setenv("TRIP_NOTIFY_MAX_PER_CYCLE", "2")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/")
    s = TripSettings.from_env()
    assert s.notify_enabled and s.notify_max_per_cycle == 2
    assert s.notify_since == datetime(2026, 10, 1, tzinfo=UTC)
    assert s.public_base_url == "https://example.test"
    assert s.notify_targets() == ("mobile_app_alice_phone",)


# --------------------------------------------------------------- websocket


def _event(action: str, event_type: str = "mobile_app_notification_action") -> dict[str, Any]:
    return {"id": 1, "type": "event", "event": {"event_type": event_type, "data": {"action": action}}}


def test_ws_url() -> None:
    assert ws_url("http://ha.test:8123/") == "ws://ha.test:8123/api/websocket"
    assert ws_url("https://ha.test") == "wss://ha.test/api/websocket"


def test_handle_event_annotates_known_trip_only() -> None:
    store = FakeStore([])
    store.mark_notified("span:9", notify_id("span:9"), "x")
    assert handle_event(_event(f"TRIP_BIZ_{notify_id('span:9')}"), store) == "span:9"
    assert handle_event(_event(f"TRIP_PRIV_{notify_id('span:9')}"), store) == "span:9"
    assert handle_event(_event(f"TRIP_BIZ_{notify_id('span:unknown')}"), store) is None
    assert handle_event(_event("KWL_HEAT_OFF"), store) is None
    assert handle_event(_event(f"TRIP_BIZ_{notify_id('span:9')}", "other_event"), store) is None
    assert handle_event({"type": "result", "success": True}, store) is None
    assert store.annotations == [
        ("span:9", {"business": True}, "notify", "ha"),
        ("span:9", {"business": False}, "notify", "ha"),
    ]


class FakeWs:
    """Scripted HA websocket: replies to auth/subscribe, then yields the
    queued events, then sets ``stop`` once drained."""

    def __init__(self, events: list[Any], stop: threading.Event, auth_ok: bool = True) -> None:
        self.inbox: list[Any] = [{"type": "auth_required", "ha_version": "2026.10.0"}]
        self.events = events
        self.stop = stop
        self.auth_ok = auth_ok
        self.sent: list[dict[str, Any]] = []

    def send(self, message: str) -> None:
        msg = json.loads(message)
        self.sent.append(msg)
        if msg["type"] == "auth":
            self.inbox.append({"type": "auth_ok" if self.auth_ok else "auth_invalid"})
        elif msg["type"] == "subscribe_events":
            self.inbox.append({"id": msg["id"], "type": "result", "success": True, "result": None})
            self.inbox.extend(self.events)

    def recv(self, timeout: float | None = None) -> str:
        if self.inbox:
            item = self.inbox.pop(0)
            return item if isinstance(item, str) else json.dumps(item)
        self.stop.set()
        raise TimeoutError

    def __enter__(self) -> FakeWs:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def test_run_session_authenticates_subscribes_and_dispatches() -> None:
    store = FakeStore([])
    store.mark_notified("span:9", notify_id("span:9"), "x")
    stop = threading.Event()
    ws = FakeWs([_event(f"TRIP_PRIV_{notify_id('span:9')}"), "not json", _event("OTHER")], stop)
    run_session(ws, "secret-token", store, stop, recv_timeout=0.01)
    assert ws.sent[0] == {"type": "auth", "access_token": "secret-token"}
    assert ws.sent[1] == {"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}
    assert store.annotations == [("span:9", {"business": False}, "notify", "ha")]


def test_run_session_auth_failure_raises() -> None:
    stop = threading.Event()
    with pytest.raises(AuthError):
        run_session(FakeWs([], stop, auth_ok=False), "bad", FakeStore([]), stop)


def test_listener_reconnects_after_drop() -> None:
    store = FakeStore([])
    store.mark_notified("span:9", notify_id("span:9"), "x")
    attempts: list[str] = []
    done = threading.Event()

    def connect(url: str) -> Any:
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionError("refused")
        ws = FakeWs([_event(f"TRIP_BIZ_{notify_id('span:9')}")], done)
        return ws

    listener = ActionListener("http://ha.test:8123", "t", store, connect=connect, max_backoff=0.05)
    listener.start()
    assert done.wait(5)
    listener.stop()
    assert attempts[:2] == ["ws://ha.test:8123/api/websocket"] * 2
    assert ("span:9", {"business": True}, "notify", "ha") in store.annotations
