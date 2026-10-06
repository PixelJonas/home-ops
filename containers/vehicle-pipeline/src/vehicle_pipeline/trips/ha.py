"""Home Assistant history client.

fetch_history is ported from trip_enricher.fetch_ha_history
(containers/trip-enricher/app/trip_enricher.py) onto httpx, with one
addition: ``significant_changes_only`` can be turned off. The odometer
needs that -- HA otherwise drops attribute-only updates of a sensor, and
the EU Data Act feed's ``data_captured_at`` attribute can change while the
km state does not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import httpx

from vehicle_pipeline.trips.util import logger, with_retry

History = dict[str, list[dict[str, Any]]]


class HistorySource(Protocol):
    def fetch_history(
        self, entity_ids: list[str], start: datetime, end: datetime, *, significant_changes_only: bool = True
    ) -> History: ...


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def fetch_history(
        self, entity_ids: list[str], start: datetime, end: datetime, *, significant_changes_only: bool = True
    ) -> History:
        entity_ids = [e for e in entity_ids if e]
        if not entity_ids:
            return {}
        url = f"{self._base}/api/history/period/{start.isoformat()}"
        params = {"filter_entity_id": ",".join(entity_ids), "end_time": end.isoformat()}
        if not significant_changes_only:
            params["significant_changes_only"] = "0"

        def call() -> Any:
            r = self._client.get(url, params=params)
            r.raise_for_status()
            return r.json()

        return parse_history_payload(with_retry(call, "ha history"))


def parse_history_payload(payload: Any) -> History:
    """HA returns one list of state dicts per entity; key them by entity_id."""
    states: History = {}
    if isinstance(payload, list):
        for entity_states in payload:
            if isinstance(entity_states, list) and entity_states:
                eid = entity_states[0].get("entity_id")
                if eid:
                    states[eid] = [s for s in entity_states if isinstance(s, dict)]
    else:
        logger.warning("unknown HA history payload type %s", type(payload).__name__)
    return states
