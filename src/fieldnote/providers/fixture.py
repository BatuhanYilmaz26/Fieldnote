"""Deterministic, explicitly synthetic fixtures for development and CI.

Nothing in this module contacts a network or claims to perform real research.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fieldnote.domain.models import excerpt_identity
from fieldnote.providers.base import ModelResult, ModelT, ProviderError
from fieldnote.services.urls import validate_public_url


class FixtureGateway:
    """Validate registered fixture payloads through the real response schema."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    async def generate(
        self,
        schema: type[ModelT],
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        *,
        reserved_cost_usd: Decimal | None = None,
    ) -> ModelResult[ModelT]:
        del messages, max_tokens, reserved_cost_usd
        payload = self._responses.get(call_identity, self._responses.get(schema.__name__))
        if payload is None:
            raise ProviderError("fixture_response_not_registered")
        self.calls.append(call_identity)
        data = schema.model_validate_json(json.dumps(payload))
        return ModelResult(
            data=data,
            requested_model="fixture/synthetic",
            resolved_model="fixture/synthetic",
            request_id=f"fixture-{hashlib.sha256(call_identity.encode()).hexdigest()[:16]}",
            provider="fixture",
            usage=None,
            cost_usd=Decimal("0"),
            fixture=True,
        )


class FixtureResearchGateway:
    """Search/read a fixed synthetic corpus supplied by the test or developer.

    Results use primitive dictionaries matching domain Source/EvidenceExcerpt
    inputs; callers validate/persist them. Empty results remain empty evidence.
    """

    def __init__(self, pages: dict[str, dict[str, str]] | None = None) -> None:
        self._pages = pages or {}
        for url, page in self._pages.items():
            validate_public_url(url)
            if not page.get("title") or not page.get("text"):
                raise ValueError("Fixture pages require title and text")

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        if not query.strip() or not 1 <= max_results <= 5:
            raise ValueError("Bounded query and result count required")
        words = set(query.lower().split())
        ranked = sorted(
            self._pages.items(),
            key=lambda item: (
                -len(words & set((item[1]["title"] + " " + item[1]["text"]).lower().split())),
                item[0],
            ),
        )
        return [
            {
                "url": url,
                "title": page["title"],
                "snippet": page["text"][:300],
                "provider": "fixture",
            }
            for url, page in ranked[:max_results]
        ]

    async def extract(self, url: str) -> dict[str, Any]:
        validate_public_url(url)
        page = self._pages.get(url)
        if page is None:
            raise ProviderError("fixture_source_not_found")
        text = page["text"][:20_000]
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        source_id = "src_" + hashlib.sha256(url.encode()).hexdigest()[:16]
        locator = f"characters 0:{len(text)} in synthetic fixture"
        return {
            "source": {
                "id": source_id,
                "url": url,
                "title": f"[Synthetic fixture] {page['title']}",
                "retrieved_at": datetime(2026, 10, 24, tzinfo=UTC).isoformat(),
                "content_hash": content_hash,
                "provider": "fixture",
                "extraction_state": "extracted",
            },
            "excerpt": {
                "id": excerpt_identity(source_id, content_hash, locator),
                "source_id": source_id,
                "text": text,
                "locator": locator,
                "content_hash": content_hash,
                "source_kind": "body",
            },
            "fixture": True,
        }
