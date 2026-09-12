from __future__ import annotations

from typing import Any

import httpx


class PaperlessClient:
    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Token {token}"},
            transport=transport,
            timeout=30.0,
        )

    async def get_document(self, doc_id: int) -> dict[str, Any]:
        resp = await self._client.get(f"/api/documents/{doc_id}/")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_tag_id(self, name: str) -> int | None:
        resp = await self._client.get("/api/tags/", params={"name__iexact": name})
        resp.raise_for_status()
        results = resp.json()["results"]
        return int(results[0]["id"]) if results else None

    async def get_tag_names(self, tag_ids: list[int]) -> dict[int, str]:
        names: dict[int, str] = {}
        for tag_id in tag_ids:
            resp = await self._client.get(f"/api/tags/{tag_id}/")
            resp.raise_for_status()
            names[tag_id] = resp.json()["name"]
        return names

    async def list_documents_by_tags_modified_since(
        self, tag_ids: list[int], since_iso: str
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            "/api/documents/",
            params={"tags__id__in": ",".join(str(t) for t in tag_ids), "modified__gte": since_iso},
        )
        resp.raise_for_status()
        return resp.json()["results"]  # type: ignore[no-any-return]

    def document_url(self, doc_id: int) -> str:
        return f"{self._base_url}/documents/{doc_id}/"

    async def aclose(self) -> None:
        await self._client.aclose()
