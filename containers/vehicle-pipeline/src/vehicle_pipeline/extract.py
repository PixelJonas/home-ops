from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

ExtractionCategory = Literal[
    "kfz_steuer",
    "haftpflicht",
    "kasko",
    "hu_au",
    "finanzierung_zinsen",
    "kraftstoff",
    "reifenwechsel",
    "autowaesche",
    "oel_betriebsstoffe",
    "adblue",
    "ersatzteile",
    "garantie",
    "not_cost_relevant",
    "other",
]

_SYSTEM_PROMPT = """Du extrahierst Kostendaten aus deutschsprachigen Fahrzeugdokumenten \
(Steuerbescheide, Versicherungen, Werkstattrechnungen, Tankquittungen, Garantien, etc.).

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt mit genau diesen Feldern:
- category: einer von kfz_steuer, haftpflicht, kasko, hu_au, finanzierung_zinsen, \
kraftstoff, reifenwechsel, autowaesche, oel_betriebsstoffe, adblue, ersatzteile, \
garantie, not_cost_relevant, other
- amount: Betrag in EUR als Zahl, oder null
- date: Datum im Format YYYY-MM-DD, oder null
- vendor: Name des Ausstellers/Anbieters, oder null
- odometer_km: Kilometerstand falls im Dokument genannt, sonst null
- notes: kurze Freitext-Notiz (max. 200 Zeichen), oder null
- confidence: high, medium oder low

Nutze "not_cost_relevant", wenn das Dokument keine Kostenposition enthält \
(z.B. reine Korrespondenz). Nutze "other", wenn ein Kostendokument keiner der \
übrigen Kategorien zugeordnet werden kann."""


class ExtractionResult(BaseModel):
    category: ExtractionCategory
    amount: float | None = None
    date: str | None = None
    vendor: str | None = None
    odometer_km: float | None = None
    notes: str | None = None
    confidence: Literal["high", "medium", "low"]


class ExtractionError(Exception):
    pass


class LiteLLMExtractor:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        # "gpt-4o-mini" was never a real registered model on this LiteLLM
        # deployment -- confirmed live 2026-09-12 via GET /v1/models against
        # this service's own scoped key, which crashed every real extraction
        # with a 400 ("Invalid model name"). gpt-5-nano is the actual
        # available lightweight/cheap model.
        model: str = "gpt-5-nano",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=60.0,
        )

    async def extract(self, document_text: str) -> ExtractionResult:
        try:
            resp = await self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": f"Dokumenttext:\n{document_text[:8000]}"},
                    ],
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # A live extraction crashed the whole reconciliation pass on a
            # transient/config LLM-gateway error (confirmed 2026-09-12: an
            # httpx.HTTPStatusError from raise_for_status() propagated
            # uncaught past this point, past process_document's
            # ExtractionError-only catch, and killed the pass instead of
            # producing the intended generic low-confidence draft). Any
            # HTTP-level failure here (4xx/5xx, timeout, connection error)
            # must degrade the same way a malformed LLM response already
            # does, not crash the caller.
            raise ExtractionError(f"LiteLLM request failed: {exc}") from exc
        raw_content = resp.json()["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"LLM did not return valid JSON: {raw_content!r}") from exc
        try:
            return ExtractionResult.model_validate(parsed)
        except ValidationError as exc:
            raise ExtractionError(f"LLM JSON failed schema validation: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
