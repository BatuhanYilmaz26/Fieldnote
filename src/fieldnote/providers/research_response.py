"""Bounded response parsing and evidence normalization for research providers."""

import hashlib
import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from fieldnote.domain.models import excerpt_identity
from fieldnote.providers.base import ProviderError, ResearchResults
from fieldnote.services.urls import canonicalize_url, validate_public_url

_INJECTION = re.compile(
    r"(ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"system\s+prompt|reveal\s+(?:the\s+)?(?:secret|api\s+key|token)|"
    r"you\s+are\s+now\s+(?:an?\s+)?(?:admin|system|developer)|"
    r"disregard\s+(?:the\s+)?(?:system|developer)\s+message)",
    re.IGNORECASE,
)
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$")


def source_identity(canonical_url: str) -> str:
    return "src_" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]


def retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            value = (date.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(value, 0.0) if math.isfinite(value) else None


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


def _last_modified(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_search(payload: Any, max_results: int, provider: str, charge: int | None) -> ResearchResults:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ProviderError("research_invalid_response", outcome_unknown=True, research_cost_micro=charge)
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
                "provider": provider,
                "published_at": None,
            }
        )
        if len(results) >= max_results:
            break
    return ResearchResults(results, charge)


def normalize_extraction(canonical: str, row: dict[str, Any], provider: str, max_chars: int, charge: int | None) -> dict[str, Any]:
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("research_extraction_empty", research_cost_micro=charge)
    text = text[: max_chars]
    final_raw = row.get("final_url") or canonical
    try:
        final = validate_public_url(str(final_raw))
    except ValueError:
        final = canonical
    title = str(row.get("title") or urlsplit(final).hostname or "Source")[:240].strip() or "Source"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    source_id = source_identity(canonical)
    locator = f"characters 0:{len(text)} of fetched Markdown"
    flags = ("possible_instruction_injection",) if _INJECTION.search(text) else ()
    published = _date(row.get("published_date"))
    modified = _last_modified(row.get("last_modified"))
    return {
        "source": {
            "id": source_id,
            "url": final,
            "original_url": canonical,
            "final_url": final,
            "title": title,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "published_at": published.isoformat() if published else None,
            **({"origin_last_modified_at": modified} if modified else {}),
            "content_hash": digest,
            "provider": provider,
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
        "cost_micro_usd": charge,
    }


async def json_body(response: httpx.Response, limit: int) -> Any:
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
