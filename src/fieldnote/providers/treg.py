"""Qualified Treg REST research adapter with explicit free-only routing.

Treg's REST contract is one fixed origin, an ``X-Treg-Token`` header and a
``/call/<operation-id>`` path. The adapter uses the catalogued TinyFish search
and fetch operations directly. Free-only operation sends a zero per-call spend
cap and disables the paid waterfall; a non-zero charge is a policy failure, not
a free result.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

import httpx

from fieldnote.providers.base import ProviderError
from fieldnote.providers.research_response import (
    json_body,
    normalize_extraction,
    normalize_search,
    retry_after,
)
from fieldnote.services.urls import canonicalize_url, resolve_public_url

_TREG_ORIGIN = "https://treg.to"
_SEARCH_OPERATION = "tinyfish.web.search"
_EXTRACT_OPERATION = "tinyfish.web.fetch"
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,1024}$")


def _micro_header(response: httpx.Response) -> int | None:
    raw = response.headers.get("x-treg-cost-micro")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 1_000_000_000 else None


def _cap(value: int) -> str:
    return f"{Decimal(value) / Decimal(1_000_000):.6f}".rstrip("0").rstrip(".") or "0"


class TregResearchGateway:
    """A bounded research gateway; it never performs hidden vendor switching."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        org: str | None = None,
        routing_policy: Literal["free_only", "budgeted"] = "free_only",
        max_cost_micro: int = 0,
        qualify_dns: bool = True,
        max_chars_per_page: int = 20_000,
    ) -> None:
        if not token or any(ch.isspace() for ch in token) or not _SAFE_TEXT.fullmatch(token):
            raise ValueError("A valid Treg token is required")
        if routing_policy not in {"free_only", "budgeted"}:
            raise ValueError("Unknown Treg routing policy")
        if not isinstance(max_cost_micro, int) or isinstance(max_cost_micro, bool) or not 0 <= max_cost_micro <= 1_000_000:
            raise ValueError("Invalid Treg route cost cap")
        if routing_policy == "free_only" and max_cost_micro != 0:
            raise ValueError("Free-only Treg routing requires a zero spend cap")
        if routing_policy == "budgeted" and max_cost_micro <= 0:
            raise ValueError("Budgeted Treg routing requires a positive spend cap")
        if org is not None and not _SAFE_TEXT.fullmatch(org):
            raise ValueError("Invalid Treg organization identity")
        if not 1000 <= max_chars_per_page <= 20_000:
            raise ValueError("Invalid page character bound")
        self._client = client
        self._token = token
        self._org = org
        self.routing_policy = routing_policy
        self.max_cost_micro = max_cost_micro
        self.qualify_dns = qualify_dns
        self.max_chars_per_page = max_chars_per_page

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-Treg-Token": self._token,
            "Accept": "application/json",
            "X-Treg-Route-Max-Cost": _cap(self.max_cost_micro),
            # A paid fallback changes spend and disclosure. Stop at the first
            # miss instead of silently walking a vendor waterfall.
            "X-Treg-Route-Waterfall": "0",
        }
        if self._org:
            headers["X-Treg-Org"] = self._org
        return headers

    def _check_charge(self, charge: int | None) -> None:
        if charge is None:
            raise ProviderError("research_billing_unknown", outcome_unknown=True)
        if self.routing_policy == "free_only" and charge != 0:
            raise ProviderError("research_cost_policy_violation", outcome_unknown=True)
        if self.routing_policy == "budgeted" and charge > self.max_cost_micro:
            raise ProviderError("research_route_cap_exceeded", outcome_unknown=True)

    def _error(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        code = {
            400: "research_request_rejected",
            401: "research_authentication_failed",
            402: "research_budget_exhausted",
            403: "research_policy_denied",
            404: "research_capability_unavailable",
            408: "research_timeout",
            429: "research_rate_limited",
        }.get(status, "research_unavailable")
        return ProviderError(
            code,
            retryable=status in {408, 429} or status >= 500,
            retry_after=retry_after(response),
            outcome_unknown=status == 408 or status >= 500,
        )

    async def _request(self, method: str, operation: str, *, limit: int, **kwargs: Any) -> tuple[Any, int | None]:
        """Carry a request-local receipt even when content or transport fails."""
        charge = None
        try:
            async with self._client.stream(
                method, f"{_TREG_ORIGIN}/call/{operation}", headers=self._headers(),
                timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False, **kwargs,
            ) as response:
                charge = _micro_header(response)
                if response.status_code != 200:
                    raise self._error(response)
                self._check_charge(charge)
                return await json_body(response, limit), charge
        except ProviderError as exc:
            exc.research_cost_micro = charge
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError("research_timeout", retryable=True, outcome_unknown=True, research_cost_micro=charge) from exc
        except httpx.RequestError as exc:
            raise ProviderError("research_connection_failed", retryable=True, outcome_unknown=True, research_cost_micro=charge) from exc

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("Bounded nonblank query required")
        if not 1 <= max_results <= 5:
            raise ValueError("At most five search results are requested")
        payload, charge = await self._request("GET", _SEARCH_OPERATION, limit=512_000,
            params={"query": query, "domain_type": "web", "page": 0})
        return normalize_search(payload, max_results, "treg/tinyfish", charge)

    async def extract(self, url: str) -> dict[str, Any]:
        canonical = canonicalize_url(url)
        if self.qualify_dns:
            await resolve_public_url(canonical)
        payload, charge = await self._request("POST", _EXTRACT_OPERATION, limit=1_048_576,
            json={"urls": [canonical], "format": "markdown", "links": False, "ttl": 0})
        if not isinstance(payload, dict):
            raise ProviderError("research_invalid_response", outcome_unknown=True, research_cost_micro=charge)
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and not payload.get("results"):
            raise ProviderError("research_extraction_failed", research_cost_micro=charge)
        rows = payload.get("results")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise ProviderError("research_extraction_failed", outcome_unknown=True, research_cost_micro=charge)
        row = rows[0]
        return normalize_extraction(canonical, row, "treg/tinyfish", self.max_chars_per_page, charge)
