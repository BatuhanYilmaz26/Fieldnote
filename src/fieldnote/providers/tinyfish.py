"""Direct TinySearch and TinyFetch for a qualified zero-cost account."""

from typing import Any

import httpx

from fieldnote.providers.base import ProviderError, ResearchResults
from fieldnote.providers.research_response import (
    json_body,
    normalize_extraction,
    normalize_search,
    retry_after,
)
from fieldnote.services.urls import (
    UnsafeURL,
    canonicalize_url,
    resolve_public_url,
    validate_public_url,
)

_SEARCH = "https://api.search.tinyfish.ai/"
_FETCH = "https://api.fetch.tinyfish.ai/"


class TinyFishResearchGateway:
    batch_size = 4

    def __init__(self, client: httpx.AsyncClient, api_key: str, *, timeout: float = 30.0,
                 max_chars_per_page: int = 20_000, qualify_dns: bool = True) -> None:
        if not api_key or not api_key.isascii() or any(not 32 < ord(char) < 127 for char in api_key):
            raise ValueError("A valid TinyFish API key is required")
        self._client = client
        self._key = api_key
        self.timeout = timeout
        self.max_chars_per_page = max_chars_per_page
        self.qualify_dns = qualify_dns

    async def _request(self, method: str, endpoint: str, *, limit: int, **kwargs: Any) -> Any:
        # The qualified account contract is the zero-price basis. A connection
        # failure without a response still has an unknown outcome in the ledger.
        received = False
        try:
            async with self._client.stream(
                method, endpoint, headers={"X-API-Key": self._key, "Accept": "application/json"},
                follow_redirects=False, timeout=httpx.Timeout(self.timeout, connect=min(5.0, self.timeout)), **kwargs,
            ) as response:
                received = True
                if response.status_code != 200:
                    code = {
                        400: "research_request_rejected", 401: "research_authentication_failed",
                        402: "research_budget_exhausted", 403: "research_policy_denied",
                        429: "research_rate_limited",
                    }.get(response.status_code, "research_unavailable")
                    raise ProviderError(code, retryable=response.status_code == 429,
                                        retry_after=retry_after(response))
                return await json_body(response, limit)
        except ProviderError as exc:
            exc.research_cost_micro = 0
            raise
        except httpx.RequestError:
            raise ProviderError("research_connection_failed", outcome_unknown=True,
                                research_cost_micro=0 if received else None) from None

    async def search(self, query: str, max_results: int = 5, *, language: str | None = None) -> ResearchResults:
        if not query.strip() or len(query) > 500 or not 1 <= max_results <= 5:
            raise ValueError("A bounded query and result count are required")
        params = {"query": query, "domain_type": "web", "page": "0"}
        if language in {"en", "tr", "es", "pt", "de"}:
            params["language"] = language
        payload = await self._request("GET", _SEARCH, params=params, limit=512_000)
        return normalize_search(payload, max_results, "tinyfish", 0)

    async def extract_many(self, urls: tuple[str, ...]) -> ResearchResults:
        if not 1 <= len(urls) <= self.batch_size:
            raise ValueError("TinyFish extraction exceeds the batch limit")
        qualified: list[str] = []
        for url in urls:
            try:
                canonical = canonicalize_url(url)
                if self.qualify_dns:
                    await resolve_public_url(canonical)
            except UnsafeURL:
                continue
            if canonical not in qualified:
                qualified.append(canonical)
        if not qualified:
            return ResearchResults([], 0)
        payload = await self._request("POST", _FETCH, limit=1_048_576, json={
            "urls": qualified, "format": "markdown", "links": False, "ttl": 0,
            "include_etag_and_last_modified": True,
            "per_url_timeout_ms": max(1, int(self.timeout * 750)),
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ProviderError("research_invalid_response", research_cost_micro=0)
        # Bind successful rows to the requested URL. Response ordering and
        # redirect destinations cannot determine the source identity.
        matched: dict[str, list[dict[str, Any]]] = {}
        for row in payload["results"]:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            try:
                canonical = canonicalize_url(row["url"])
            except UnsafeURL:
                continue
            if canonical in qualified:
                matched.setdefault(canonical, []).append(row)
        extracted = []
        for canonical in qualified:
            rows = matched.get(canonical, [])
            if len(rows) != 1:
                continue
            row = rows[0]
            try:
                if row.get("final_url") is not None:
                    validate_public_url(str(row["final_url"]))
                extracted.append(normalize_extraction(canonical, row, "tinyfish", self.max_chars_per_page, 0))
            except (ProviderError, UnsafeURL):
                continue
        return ResearchResults(extracted, 0)

    async def extract(self, url: str) -> dict[str, Any]:
        rows = await self.extract_many((url,))
        if not rows:
            raise ProviderError("research_extraction_failed", research_cost_micro=0)
        return rows[0]
