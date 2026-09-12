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
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), transport=transport, timeout=30.0
        )

    async def create_record(self, vin: str, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = _ENTITY_PATHS[entity]
        token = await self._get_token()
        resp = await self._post_record(vin, path, payload, token)
        if resp.status_code == 401:
            self._token = None
            token = await self._get_token()
            resp = await self._post_record(vin, path, payload, token)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _post_record(
        self, vin: str, path: str, payload: dict[str, Any], token: str
    ) -> httpx.Response:
        return await self._client.post(
            f"/api/vehicles/{vin}/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _get_token(self) -> str:
        if self._token is None:
            resp = await self._client.post(
                "/api/auth/login",
                json={"username": self._username, "password": self._password},
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
        return self._token

    async def aclose(self) -> None:
        await self._client.aclose()
