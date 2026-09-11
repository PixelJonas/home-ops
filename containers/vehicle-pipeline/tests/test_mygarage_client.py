import httpx
import pytest

from vehicle_pipeline.mygarage_client import MyGarageClient


@pytest.mark.asyncio
async def test_create_record_logs_in_then_posts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/auth/login":
            body = httpx.Request("POST", request.url, content=request.content)
            import json as _json
            assert _json.loads(request.content) == {"username": "admin", "password": "adminpw"}
            return httpx.Response(200, json={"access_token": "tok123", "token_type": "bearer",
                                              "expires_in": 3600, "csrf_token": "csrf"})
        assert request.url.path == "/api/vehicles/WVGZZZE27SE017858/tax-records"
        assert request.headers["Authorization"] == "Bearer tok123"
        return httpx.Response(201, json={"id": 99, "amount": 120.5})

    client = MyGarageClient(
        "https://mygarage.example.test", "admin", "adminpw", transport=httpx.MockTransport(handler)
    )
    result = await client.create_record(
        "WVGZZZE27SE017858", "tax-records",
        {"vin": "WVGZZZE27SE017858", "date": "2026-03-01", "amount": 120.5},
    )
    assert result == {"id": 99, "amount": 120.5}
    assert calls == ["/api/auth/login", "/api/vehicles/WVGZZZE27SE017858/tax-records"]
    await client.aclose()


@pytest.mark.asyncio
async def test_create_record_reuses_cached_token() -> None:
    login_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        if request.url.path == "/api/auth/login":
            login_calls += 1
            return httpx.Response(200, json={"access_token": "tok123", "token_type": "bearer",
                                              "expires_in": 3600, "csrf_token": "csrf"})
        return httpx.Response(201, json={"id": 1})

    client = MyGarageClient(
        "https://mygarage.example.test", "admin", "adminpw", transport=httpx.MockTransport(handler)
    )
    await client.create_record("VIN1", "def", {"vin": "VIN1", "date": "2026-01-01"})
    await client.create_record("VIN1", "def", {"vin": "VIN1", "date": "2026-01-02"})
    assert login_calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_create_record_retries_once_on_401() -> None:
    login_calls = 0
    post_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls, post_attempts
        if request.url.path == "/api/auth/login":
            login_calls += 1
            token = "tok-stale" if login_calls == 1 else "tok-fresh"
            return httpx.Response(200, json={"access_token": token, "token_type": "bearer",
                                              "expires_in": 3600, "csrf_token": "csrf"})
        assert request.url.path == "/api/vehicles/VIN1/def"
        post_attempts += 1
        if post_attempts == 1:
            assert request.headers["Authorization"] == "Bearer tok-stale"
            return httpx.Response(401, json={"detail": "token expired"})
        assert request.headers["Authorization"] == "Bearer tok-fresh"
        return httpx.Response(201, json={"id": 5})

    client = MyGarageClient(
        "https://mygarage.example.test", "admin", "adminpw", transport=httpx.MockTransport(handler)
    )
    result = await client.create_record("VIN1", "def", {"vin": "VIN1", "date": "2026-01-01"})
    assert result == {"id": 5}
    assert login_calls == 2
    assert post_attempts == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_create_record_raises_if_retry_also_401s() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "tok-bad", "token_type": "bearer",
                                              "expires_in": 3600, "csrf_token": "csrf"})
        return httpx.Response(401, json={"detail": "token expired"})

    client = MyGarageClient(
        "https://mygarage.example.test", "admin", "adminpw", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_record("VIN1", "def", {"vin": "VIN1", "date": "2026-01-01"})
    await client.aclose()
