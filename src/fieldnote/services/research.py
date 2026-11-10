"""Bounded query selection, source ranking, deduplication and evidence packing."""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from fieldnote.domain.languages import fold_for_matching
from fieldnote.providers.base import ProviderError
from fieldnote.services.urls import canonicalize_url

_WORD = re.compile(r"[^\W_](?:[^\W_]|['’-]){1,40}")
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
_STOP_ERRORS = frozenset({
    "research_authentication_failed", "research_policy_denied",
    "research_cost_policy_violation", "research_budget_exhausted",
    "research_billing_unknown", "research_route_cap_exceeded",
})
_STOP = frozenset(
    "the and for with from that this into about your you our their its are was were will "
    "have has been being over under more most less least than then when where which while "
    "what who whom whose how why can could should would may might must not but also".split()
)


class ResearchGateway(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]: ...

    async def extract(self, url: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    max_queries: int = 4
    max_results_per_query: int = 5
    max_pages: int = 8
    max_chars_per_page: int = 20_000
    max_evidence_tokens: int = 12_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_queries <= 4:
            raise ValueError("At most four research queries are permitted")
        if not 1 <= self.max_results_per_query <= 5:
            raise ValueError("At most five results per query are permitted")
        if not 1 <= self.max_pages <= 8:
            raise ValueError("At most eight pages may be extracted")
        if not 1000 <= self.max_chars_per_page <= 20_000:
            raise ValueError("Invalid page character bound")
        if not 1024 <= self.max_evidence_tokens <= 12_000:
            raise ValueError("Invalid evidence token bound")


@dataclass(slots=True)
class ResearchOutcome:
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    excerpts: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    duplicate_urls: int = 0

    def limitations(self) -> list[str]:
        values: list[str] = []
        if self.failures:
            values.append(f"{len(self.failures)} research page(s) could not be extracted.")
        if self.truncated:
            values.append("The research pack was bounded before every candidate page could be used.")
        if self.duplicate_urls:
            values.append(f"{self.duplicate_urls} duplicate search result(s) were ignored.")
        return values


def _tokens(value: str) -> set[str]:
    normalized = fold_for_matching(value)
    return {word for word in _WORD.findall(normalized) if word not in _STOP}


def _authority(url: str) -> int:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    score = 0
    if host.endswith((".gov", ".edu", ".int")):
        score += 4
    if host.startswith(("docs.", "developer.", "developers.", "support.", "help.")):
        score += 3
    if host in {"github.com", "wikipedia.org"} or host.endswith(".wikipedia.org"):
        score += 1
    depth = len([part for part in parts.path.split("/") if part])
    if depth <= 2:
        score += 1
    return score


def rank_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by lexical relevance, primary-source signals and host diversity."""
    wanted = _tokens(query)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, result in enumerate(results):
        url = result.get("url", "")
        text = f"{result.get('title', '')} {result.get('snippet', '')}"
        overlap = len(wanted & _tokens(text)) / max(1, len(wanted))
        score = overlap * 6 + _authority(url)
        scored.append((score, index, result))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ordered: list[dict[str, Any]] = []
    host_counts: dict[str, int] = {}
    for score, _, result in scored:
        host = (urlsplit(result.get("url", "")).hostname or "").lower()
        penalty = host_counts.get(host, 0) * 2.5
        host_counts[host] = host_counts.get(host, 0) + 1
        ordered.append({**result, "_rank": score - penalty})
    ordered.sort(key=lambda item: -float(item["_rank"]))
    return ordered


def pack_evidence(
    excerpts: dict[str, dict[str, Any]],
    goal: str,
    *,
    max_tokens: int = 3000,
    limit: int = 5,
) -> dict[str, dict[str, Any]]:
    """Select a relevant, bounded subset for one section; never fabricate text."""
    if type(max_tokens) is not int or not 1 <= max_tokens <= 12_000:
        raise ValueError("Invalid evidence pack bound")
    if type(limit) is not int or not 1 <= limit <= 24:
        raise ValueError("Invalid evidence count bound")
    wanted = _tokens(goal)
    ranked = sorted(
        excerpts.items(),
        key=lambda item: (
            -len(wanted & _tokens(str(item[1].get("text", "")))),
            -len(str(item[1].get("text", ""))),
            item[0],
        ),
    )
    chosen: dict[str, dict[str, Any]] = {}
    used = 0
    for excerpt_id, excerpt in ranked:
        estimated = max(1, math.ceil(len(str(excerpt.get("text", "")).encode("utf-8")) / 4))
        if used + estimated > max_tokens:
            continue
        chosen[excerpt_id] = excerpt
        used += estimated
        if used >= max_tokens or len(chosen) >= limit:
            break
    return chosen


def relevant_evidence_ids(
    goal: str, excerpts: dict[str, dict[str, Any]], *, limit: int = 3
) -> tuple[str, ...]:
    return tuple(pack_evidence(excerpts, goal, max_tokens=4000, limit=limit))


def flag_conflicts(excerpts: dict[str, dict[str, Any]]) -> None:
    """Add a conservative possible-conflict flag when numbers disagree.

    This is a review prompt, not an entailment or truth judgement. It only
    compares excerpts that already share several content tokens.
    """
    values = list(excerpts.items())
    for left_index, (left_id, left) in enumerate(values):
        left_tokens = _tokens(str(left.get("text", "")))
        left_numbers = set(_NUMBER.findall(str(left.get("text", ""))))
        if not left_numbers:
            continue
        for right_id, right in values[left_index + 1 :]:
            shared = left_tokens & _tokens(str(right.get("text", "")))
            if len(shared) < 4:
                continue
            right_numbers = set(_NUMBER.findall(str(right.get("text", ""))))
            if right_numbers and left_numbers != right_numbers:
                for _excerpt_id, excerpt in ((left_id, left), (right_id, right)):
                    flags = list(excerpt.get("flags", []))
                    if "possible_numeric_conflict" not in flags:
                        flags.append("possible_numeric_conflict")
                    excerpt["flags"] = flags


class ResearchCoordinator:
    """Execute at most four queries and eight extractions per required run."""

    def __init__(
        self,
        gateway: ResearchGateway,
        limits: ResearchLimits | None = None,
        *,
        check_active: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.gateway = gateway
        self.limits = limits or ResearchLimits()
        self.check_active = check_active

    async def collect(
        self,
        queries: tuple[str, ...] | list[str],
        *,
        goal: str = "",
        reference_urls: tuple[str, ...] = (),
    ) -> ResearchOutcome:
        bounded: list[str] = []
        seen_queries: set[str] = set()
        for query in queries:
            clean = query.strip()
            key = clean.casefold()
            if clean and key not in seen_queries and len(bounded) < self.limits.max_queries:
                seen_queries.add(key)
                bounded.append(clean)
        if not bounded:
            return ResearchOutcome()

        outcome = ResearchOutcome()
        candidates: dict[str, dict[str, Any]] = {}
        for url in reference_urls[:4]:
            normalized = canonicalize_url(url)
            candidates[normalized] = {"url": normalized, "_rank": 100, "selected_by_author": True}
        for query in bounded:
            # Admission errors must escape page-failure handling. The worker
            # checks durable cancellation, deadline and lease ownership here.
            if self.check_active is not None:
                await self.check_active()
            try:
                found = await self.gateway.search(
                    query, max_results=self.limits.max_results_per_query
                )
            except ProviderError as exc:
                if exc.code in _STOP_ERRORS:
                    raise
                outcome.failures.append({"stage": "search", "code": exc.code})
                continue
            for result in rank_results(query, found):
                try:
                    url = canonicalize_url(str(result["url"]))
                except (KeyError, ValueError):
                    continue
                if url in candidates:
                    outcome.duplicate_urls += 1
                    continue
                candidates[url] = {**result, "url": url}

        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                -float(item.get("_rank", 0)),
                str(item.get("url", "")),
            ),
        )
        if len(ranked) > self.limits.max_pages:
            outcome.truncated = True
        used_tokens = 0
        for result in ranked[: self.limits.max_pages]:
            if self.check_active is not None:
                await self.check_active()
            try:
                extracted = await self.gateway.extract(str(result["url"]))
            except ProviderError as exc:
                if exc.code in _STOP_ERRORS:
                    raise
                outcome.failures.append({"stage": "extract", "code": exc.code})
                continue
            source = extracted.get("source")
            excerpt = extracted.get("excerpt")
            if not isinstance(source, dict) or not isinstance(excerpt, dict):
                outcome.failures.append({"stage": "extract", "code": "research_invalid_response"})
                continue
            source_id = str(source.get("id", ""))
            excerpt_id = str(excerpt.get("id", ""))
            if not source_id or not excerpt_id:
                outcome.failures.append({"stage": "extract", "code": "research_invalid_response"})
                continue
            if str(excerpt.get("source_id", "")) != source_id:
                raise ProviderError("research_evidence_identity_conflict")
            previous = outcome.excerpts.get(excerpt_id)
            if previous is not None and any(
                previous.get(key) != excerpt.get(key)
                for key in ("source_id", "content_hash", "locator", "text")
            ):
                raise ProviderError("research_evidence_identity_conflict")
            text = str(excerpt.get("text", ""))
            estimated = max(1, math.ceil(len(text.encode("utf-8")) / 4))
            if (len(text) > self.limits.max_chars_per_page
                    or used_tokens + estimated > self.limits.max_evidence_tokens):
                # Keep text/hash/locator identity intact. A smaller later page
                # may still fit; never force the first oversized page through.
                outcome.truncated = True
                continue
            excerpt = {**excerpt}
            flags = list(excerpt.get("flags", []))
            if source.get("published_at") is None and "publication_date_unknown" not in flags:
                flags.append("publication_date_unknown")
            excerpt["flags"] = flags
            outcome.sources[source_id] = source
            outcome.excerpts[excerpt_id] = excerpt
            used_tokens += estimated
        flag_conflicts(outcome.excerpts)
        return outcome
