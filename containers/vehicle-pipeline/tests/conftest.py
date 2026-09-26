from __future__ import annotations

from typing import Any

import pytest

from vehicle_pipeline.config import Settings


class FakeIngestEventStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def record_event(self, event_id: str, source: str, payload: dict[str, Any]) -> bool:
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        return True


@pytest.fixture
def fake_ingest_store() -> FakeIngestEventStore:
    return FakeIngestEventStore()


class FakeReviewStore:
    """Mirrors ReviewQueueStore.create_draft's real idempotency: a `pending`
    row for the same paperless_doc_id is reused rather than duplicated."""

    def __init__(self) -> None:
        self.drafts: list[dict[str, Any]] = []
        self._next_id = 1

    def create_draft(self, *, paperless_doc_id: int, **kwargs: Any) -> int:
        for existing in self.drafts:
            if existing["paperless_doc_id"] == paperless_doc_id:
                return int(existing["id"])
        item_id = self._next_id
        self._next_id += 1
        self.drafts.append({"id": item_id, "paperless_doc_id": paperless_doc_id, **kwargs})
        return item_id


@pytest.fixture
def fake_review_store() -> FakeReviewStore:
    return FakeReviewStore()


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="postgresql://test/test",
        paperless_url="https://paperless.example.test",
        paperless_token="ptoken",
        paperless_webhook_secret="whsecret",
        litellm_base_url="http://litellm.internal/v1",
        litellm_api_key="llmkey",
        mygarage_url="https://mygarage.example.test",
        mygarage_username="admin",
        mygarage_password="adminpw",
        vehicles={"WVGZZZE27SE017858": "id4", "WV2ZZZ7HZNH000000": "multivan"},
        ingestbuddy_handoff_secret="handoffsecret",
        poll_interval_seconds=900,
    )
