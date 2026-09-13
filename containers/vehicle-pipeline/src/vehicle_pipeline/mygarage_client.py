from __future__ import annotations

from typing import Any

import httpx

_ENTITY_PATHS = {
    "fuel": "fuel",
    "service-visits": "service-visits",
    "insurance": "insurance",
    "warranties": "warranties",
    "tax-records": "tax-records",
    "def": "def",
    "documents": "documents",
}


class MyGarageClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._token: str | None = None
        self._csrf_token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), transport=transport, timeout=30.0
        )

    async def create_record(self, vin: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = _ENTITY_PATHS[entity]
        token, csrf = await self._get_tokens()
        resp = await self._post_record(vin, path, payload, token, csrf)
        # Confirmed live 2026-09-12 backfilling real Multivan cost records:
        # every write -- not just an expired-token case -- was rejected
        # with `{"detail": "CSRF token missing. Include X-CSRF-Token
        # header with your request."}` until this header was added. This
        # was never exercised by any prior test (they all mocked the
        # transport), so the review UI's approve button was broken for
        # every entity type against the real deployment.
        if resp.status_code in (401, 403):
            self._token = None
            self._csrf_token = None
            token, csrf = await self._get_tokens()
            resp = await self._post_record(vin, path, payload, token, csrf)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _post_record(
        self, vin: str, path: str, payload: dict[str, Any], token: str, csrf: str
    ) -> httpx.Response:
        return await self._client.post(
            f"/api/vehicles/{vin}/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
        )

    async def _get_tokens(self) -> tuple[str, str]:
        if self._token is None or self._csrf_token is None:
            resp = await self._client.post(
                "/api/auth/login",
                json={"username": self._username, "password": self._password},
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._csrf_token = body["csrf_token"]
        return self._token, self._csrf_token

    async def aclose(self) -> None:
        await self._client.aclose()
