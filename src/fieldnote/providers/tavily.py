"""Optional direct Tavily adapter, never activated by a hidden fallback."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx

from fieldnote.domain.models import excerpt_identity
from fieldnote.providers.base import ProviderError
from fieldnote.services.urls import canonicalize_url, resolve_public_url, validate_public_url

_ORIGIN = "https://api.tavily.com"


def _error(response: httpx.Response) -> ProviderError:
    code = {
        400: "research_request_rejected",
        401: "research_authentication_failed",
        402: "research_budget_exhausted",
        403: "research_policy_denied",
        429: "research_rate_limited",
    }.get(response.status_code, "research_unavailable")
    return ProviderError(
        code,
        retryable=response.status_code == 429 or response.status_code >= 500,
        outcome_unknown=response.status_code >= 500,
    )


async def _json(response: httpx.Response, limit: int) -> Any:
    import json

    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise ProviderError("research_response_too_large", outcome_unknown=True)
    try:
        return json.loads(chunks)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("research_invalid_response", outcome_unknown=True) from exc


class TavilyResearchGateway:
    """A directly selected Tavily adapter with bounded requests and validation."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        qualify_dns: bool = True,
        max_chars_per_page: int = 20_000,
    ) -> None:
        if not api_key or any(ch.isspace() for ch in api_key):
            raise ValueError("A valid Tavily API key is required")
        if not 1000 <= max_chars_per_page <= 20_000:
            raise ValueError("Invalid page character bound")
        self._client = client
        self._key = api_key
        self.qualify_dns = qualify_dns
        self.max_chars_per_page = max_chars_per_page

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("Bounded nonblank query required")
        if not 1 <= max_results <= 5:
            raise ValueError("At most five search results are requested")
        try:
            async with self._client.stream(
                "POST",
                f"{_ORIGIN}/search",
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": False,
                    "include_raw_content": False,
                },
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise _error(response)
                payload = await _json(response, 512_000)
        except httpx.TimeoutException as exc:
            raise ProviderError("research_timeout", retryable=True, outcome_unknown=True) from exc
        except httpx.RequestError as exc:
            raise ProviderError("research_connection_failed", retryable=True, outcome_unknown=True) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ProviderError("research_invalid_response", outcome_unknown=True)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            try:
                url = canonicalize_url(item["url"])
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            rows.append(
                {
                    "url": url,
                    "title": str(item.get("title") or "Source")[:240],
                    "snippet": str(item.get("content") or "")[:1000],
                    "provider": "tavily",
                    "published_at": None,
                }
            )
            if len(rows) >= max_results:
                break
        return rows

    async def extract(self, url: str) -> dict[str, Any]:
        canonical = canonicalize_url(url)
        if self.qualify_dns:
            await resolve_public_url(canonical)
        try:
            async with self._client.stream(
                "POST",
                f"{_ORIGIN}/extract",
                headers={"Authorization": f"Bearer {self._key}"},
                json={"urls": [canonical], "extract_depth": "basic", "format": "markdown"},
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise _error(response)
                payload = await _json(response, 1_048_576)
        except httpx.TimeoutException as exc:
            raise ProviderError("research_timeout", retryable=True, outcome_unknown=True) from exc
        except httpx.RequestError as exc:
            raise ProviderError("research_connection_failed", retryable=True, outcome_unknown=True) from exc
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ProviderError("research_extraction_failed")
        row = next((item for item in rows if isinstance(item, dict)), None)
        if row is None:
            raise ProviderError("research_extraction_failed")
        text = row.get("raw_content") or row.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("research_extraction_empty")
        text = text[: self.max_chars_per_page]
        final = canonical
        try:
            final = validate_public_url(str(row.get("url") or canonical))
        except ValueError:
            pass
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_id = "src_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        locator = f"characters 0:{len(text)} of extracted Markdown"
        return {
            "source": {
                "id": source_id,
                "url": final,
                "original_url": canonical,
                "final_url": final,
                "title": str(row.get("title") or "Source")[:240],
                "retrieved_at": datetime.now(UTC).isoformat(),
                "content_hash": digest,
                "provider": "tavily",
                "extraction_state": "extracted",
            },
            "excerpt": {
                "id": excerpt_identity(source_id, digest, locator),
                "source_id": source_id,
                "text": text,
                "locator": locator,
                "content_hash": digest,
                "source_kind": "body",
                "flags": [],
            },
            "fixture": False,
        }
