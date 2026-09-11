import httpx
import pytest

from vehicle_pipeline.paperless_client import PaperlessClient


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_document_returns_raw_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/42/"
        assert request.headers["Authorization"] == "Token ptoken"
        return httpx.Response(200, json={"id": 42, "title": "KFZ-Steuer", "content": "text", "tags": [5, 9]})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    doc = await client.get_document(42)
    assert doc["title"] == "KFZ-Steuer"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_tag_id_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags/"
        assert request.url.params["name__iexact"] == "Fahrzeug:ID4-auto"
        return httpx.Response(200, json={"results": [{"id": 7, "name": "Fahrzeug:ID4-auto"}]})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    tag_id = await client.get_tag_id("Fahrzeug:ID4-auto")
    assert tag_id == 7
    await client.aclose()


@pytest.mark.asyncio
async def test_get_tag_id_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    assert await client.get_tag_id("Fahrzeug:Missing") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_get_tag_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags/7/":
            return httpx.Response(200, json={"id": 7, "name": "Fahrzeug:ID4-auto"})
        if request.url.path == "/api/tags/9/":
            return httpx.Response(200, json={"id": 9, "name": "Fahrzeug:Multivan-auto"})
        return httpx.Response(404)

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    names = await client.get_tag_names([7, 9])
    assert names == {7: "Fahrzeug:ID4-auto", 9: "Fahrzeug:Multivan-auto"}
    await client.aclose()


@pytest.mark.asyncio
async def test_list_documents_by_tags_modified_since() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/"
        assert request.url.params["tags__id__in"] == "7,9"
        assert request.url.params["modified__gte"] == "2026-09-01T00:00:00+00:00"
        return httpx.Response(200, json={"results": [{"id": 1}, {"id": 2}]})

    client = PaperlessClient("https://paperless.example.test", "ptoken", transport=_transport(handler))
    docs = await client.list_documents_by_tags_modified_since([7, 9], "2026-09-01T00:00:00+00:00")
    assert [d["id"] for d in docs] == [1, 2]
    await client.aclose()
