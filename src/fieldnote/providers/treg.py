"""Qualified Treg REST research adapter with explicit free-only routing.

Treg's REST contract is one fixed origin, an ``X-Treg-Token`` header and a
``/call/<operation-id>`` path. The adapter uses the catalogued TinyFish search
and fetch operations directly. Free-only operation sends a zero per-call spend
cap and disables the paid waterfall; a non-zero charge is a policy failure, not
a free result.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from fieldnote.domain.models import excerpt_identity
from fieldnote.providers.base import ProviderError
from fieldnote.services.urls import canonicalize_url, resolve_public_url, validate_public_url

_TREG_ORIGIN = "https://treg.to"
_SEARCH_OPERATION = "tinyfish.web.search"
_EXTRACT_OPERATION = "tinyfish.web.fetch"
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,1024}$")
_INJECTION = re.compile(
    r"(ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"system\s+prompt|reveal\s+(?:the\s+)?(?:secret|api\s+key|token)|"
    r"you\s+are\s+now\s+(?:an?\s+)?(?:admin|system|developer)|"
    r"disregard\s+(?:the\s+)?(?:system|developer)\s+message)",
    re.IGNORECASE,
)
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$")


def _micro_header(response: httpx.Response) -> int | None:
    raw = response.headers.get("x-treg-cost-micro")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 1_000_000_000 else None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return min(max(value, 0.0), 300.0)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = _DATE.fullmatch(value.strip())
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
    except ValueError:
        return None


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
        self.last_charge_micro = 0

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

    def _check_charge(self, response: httpx.Response) -> int:
        charge = _micro_header(response)
        if charge is None:
            raise ProviderError("research_billing_unknown", outcome_unknown=True)
        self.last_charge_micro = charge
        if self.routing_policy == "free_only" and charge != 0:
            raise ProviderError("research_cost_policy_violation", outcome_unknown=True)
        if self.routing_policy == "budgeted" and charge > self.max_cost_micro:
            raise ProviderError("research_route_cap_exceeded", outcome_unknown=True)
        return charge

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
            retry_after=_retry_after(response),
            outcome_unknown=status == 408 or status >= 500,
        )

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("Bounded nonblank query required")
        if not 1 <= max_results <= 5:
            raise ValueError("At most five search results are requested")
        try:
            async with self._client.stream(
                "GET",
                f"{_TREG_ORIGIN}/call/{_SEARCH_OPERATION}",
                headers=self._headers(),
                params={"query": query, "domain_type": "web", "page": 0},
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise self._error(response)
                self._check_charge(response)
                payload = await _json_body(response, 512_000)
        except httpx.TimeoutException as exc:
            raise ProviderError("research_timeout", retryable=True, outcome_unknown=True) from exc
        except httpx.RequestError as exc:
            raise ProviderError("research_connection_failed", retryable=True, outcome_unknown=True) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ProviderError("research_invalid_response", outcome_unknown=True)
        results: list[dict[str, Any]] = []
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
            title = str(item.get("title") or urlsplit(url).hostname or "Source")[:240]
            snippet = str(item.get("snippet") or "")[:1000]
            results.append(
                {
                    "url": url,
                    "title": title.strip() or "Source",
                    "snippet": snippet.strip(),
                    "provider": "treg/tinyfish",
                    "published_at": None,
                }
            )
            if len(results) >= max_results:
                break
        return results

    async def extract(self, url: str) -> dict[str, Any]:
        canonical = canonicalize_url(url)
        if self.qualify_dns:
            await resolve_public_url(canonical)
        try:
            async with self._client.stream(
                "POST",
                f"{_TREG_ORIGIN}/call/{_EXTRACT_OPERATION}",
                headers=self._headers(),
                json={"urls": [canonical], "format": "markdown", "links": False, "ttl": 0},
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise self._error(response)
                self._check_charge(response)
                payload = await _json_body(response, 1_048_576)
        except httpx.TimeoutException as exc:
            raise ProviderError("research_timeout", retryable=True, outcome_unknown=True) from exc
        except httpx.RequestError as exc:
            raise ProviderError("research_connection_failed", retryable=True, outcome_unknown=True) from exc
        if not isinstance(payload, dict):
            raise ProviderError("research_invalid_response", outcome_unknown=True)
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and not payload.get("results"):
            raise ProviderError("research_extraction_failed")
        rows = payload.get("results")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise ProviderError("research_extraction_failed", outcome_unknown=True)
        row = rows[0]
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("research_extraction_empty")
        text = text[: self.max_chars_per_page]
        final_raw = row.get("final_url") or canonical
        try:
            final = validate_public_url(str(final_raw))
        except ValueError:
            final = canonical
        title = str(row.get("title") or urlsplit(final).hostname or "Source")[:240].strip() or "Source"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_id = "src_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        locator = f"characters 0:{len(text)} of fetched Markdown"
        flags = ("possible_instruction_injection",) if _INJECTION.search(text) else ()
        published = _date(row.get("published_date"))
        return {
            "source": {
                "id": source_id,
                "url": final,
                "original_url": canonical,
                "final_url": final,
                "title": title,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "published_at": published.isoformat() if published else None,
                "content_hash": digest,
                "provider": "treg/tinyfish",
                "extraction_state": "extracted",
            },
            "excerpt": {
                "id": excerpt_identity(source_id, digest, locator),
                "source_id": source_id,
                "text": text,
                "locator": locator,
                "content_hash": digest,
                "source_kind": "body",
                "flags": list(flags),
            },
            "fixture": False,
            "cost_micro_usd": self.last_charge_micro,
        }


async def _json_body(response: httpx.Response, limit: int) -> Any:
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
