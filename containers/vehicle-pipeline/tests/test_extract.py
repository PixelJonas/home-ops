import json

import httpx
import pytest

from vehicle_pipeline.extract import ExtractionError, ExtractionResult, LiteLLMExtractor


def _chat_response(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
    )


@pytest.mark.asyncio
async def test_extract_parses_valid_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer llmkey"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["response_format"] == {"type": "json_object"}
        return _chat_response(
            {
                "category": "kfz_steuer",
                "amount": 120.5,
                "date": "2026-03-01",
                "vendor": "Hauptzollamt",
                "odometer_km": None,
                "notes": "Jahressteuer 2026",
                "confidence": "high",
            }
        )

    extractor = LiteLLMExtractor(
        "http://litellm.internal/v1", "llmkey", transport=httpx.MockTransport(handler)
    )
    result = await extractor.extract("KFZ-Steuerbescheid ... 120,50 EUR ...")
    assert isinstance(result, ExtractionResult)
    assert result.category == "kfz_steuer"
    assert result.amount == 120.5
    await extractor.aclose()


@pytest.mark.asyncio
async def test_extract_raises_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    extractor = LiteLLMExtractor(
        "http://litellm.internal/v1", "llmkey", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ExtractionError):
        await extractor.extract("some text")
    await extractor.aclose()


@pytest.mark.asyncio
async def test_extract_raises_on_invalid_category() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response({"category": "not_a_real_category", "confidence": "high"})

    extractor = LiteLLMExtractor(
        "http://litellm.internal/v1", "llmkey", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ExtractionError):
        await extractor.extract("some text")
    await extractor.aclose()
