from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    paperless_url: str
    paperless_token: str
    paperless_webhook_secret: str
    litellm_base_url: str
    litellm_api_key: str
    mygarage_url: str
    mygarage_username: str
    mygarage_password: str
    vehicles: dict[str, str]
    poll_interval_seconds: int = 900

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_require("DATABASE_URL"),
            paperless_url=_require("PAPERLESS_URL").rstrip("/"),
            paperless_token=_require("PAPERLESS_TOKEN"),
            paperless_webhook_secret=_require("PAPERLESS_WEBHOOK_SECRET"),
            litellm_base_url=_require("LITELLM_BASE_URL").rstrip("/"),
            litellm_api_key=_require("LITELLM_API_KEY"),
            mygarage_url=_require("MYGARAGE_URL").rstrip("/"),
            mygarage_username=_require("MYGARAGE_USERNAME"),
            mygarage_password=_require("MYGARAGE_PASSWORD"),
            vehicles=json.loads(_require("MYGARAGE_VEHICLES")),
            poll_interval_seconds=_parse_interval(os.environ.get("POLL_INTERVAL", "15m")),
        )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _parse_interval(raw: str) -> int:
    raw = raw.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1]) * 60
    if raw.endswith("s"):
        return int(raw[:-1])
    return int(raw)
