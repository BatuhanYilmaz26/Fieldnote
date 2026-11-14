"""One durable reservation per outbound research attempt, with no hidden retry."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fieldnote.providers.base import DispatchAdmissionError, ProviderError, ResearchResults
from fieldnote.providers.research_response import source_identity
from fieldnote.providers.tinyfish import TinyFishResearchGateway
from fieldnote.services.budgets import BudgetError, BudgetService
from fieldnote.services.research import ResearchGateway
from fieldnote.services.urls import UnsafeURL, canonicalize_url
from fieldnote.settings import Settings
from fieldnote.storage.repository import Claim, markdown_hash


class ResearchDispatchError(ProviderError, DispatchAdmissionError):
    """A request failure that must release the job to its single retry owner."""


def _cost(micro: Any) -> Decimal | None:
    if type(micro) is not int or not 0 <= micro <= 1_000_000_000:
        return None
    return Decimal(micro) / Decimal(1_000_000)


class AccountedResearch:
    def __init__(self, gateway: ResearchGateway, budget: BudgetService, claim: Claim, settings: Settings,
                 *, language: str | None = None, cache_seconds: int = 0) -> None:
        self.gateway = gateway
        self.budget = budget
        self.claim = claim
        self.settings = settings
        self.sequence = 0
        self.language = language
        self.cache_seconds = cache_seconds
        self.batch_size = gateway.batch_size if isinstance(gateway, TinyFishResearchGateway) else 1

    async def _dispatch[T](self, operation: str, target: str, call: Callable[[], Awaitable[T]], *, urls: int = 0) -> T:
        # A route cap is the qualified conservative monetary bound, not an
        # assumed average price. Unpriced adapters do not inherit zero cost.
        provider = self.settings.research_provider
        if provider not in {"treg", "tinyfish"}:
            raise BudgetError("research_price_unknown")
        cap = self.settings.treg_max_cost_micro if provider == "treg" else 0
        overrun_code = (
            "research_route_cap_exceeded" if provider == "treg" and self.settings.treg_routing_policy == "budgeted"
            else "research_cost_policy_violation"
        )
        self.sequence += 1
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
        identity = f"research/{self.claim.generation}/{self.sequence}/{operation}/{digest}"
        try:
            reservation = await self.budget.reserve_call(
                self.claim, identity, max_tokens=0,
                estimated_cost_usd=Decimal(cap) / Decimal(1_000_000),
                url_count=urls,
            )
        except BudgetError as exc:
            raise ResearchDispatchError(exc.code, retryable=exc.retryable, retry_after=exc.retry_after) from None
        if not reservation.dispatch_allowed:
            raise BudgetError("research_dispatch_already_reserved", outcome_unknown=True)
        metadata = {"provider": provider, "operation": operation}
        try:
            result = await call()
        except BaseException as exc:
            cost = _cost(exc.research_cost_micro) if isinstance(exc, ProviderError) else None
            if isinstance(exc, UnsafeURL):
                cost = Decimal(0)  # The adapter rejects the URL before dispatch.
            rate_limited = isinstance(exc, ProviderError) and exc.code == "research_rate_limited"
            accounting = await self.budget.reconcile(reservation, usage_tokens=0, actual_cost_usd=cost,
                outcome="unknown" if cost is None or getattr(exc, "outcome_unknown", False) else "failed",
                retryable=bool(getattr(exc, "retryable", False)),
                retry_after=exc.retry_after if rate_limited and isinstance(exc, ProviderError) else None,
                metadata={**metadata, "code": getattr(exc, "code", "research_dispatch_interrupted")})
            if not isinstance(exc, Exception):
                raise  # Cancellation is still cancellation after recording its uncertain bill.
            if cost is None:
                raise ProviderError("research_billing_unknown", outcome_unknown=True) from None
            if accounting.reservation_overrun:
                raise ProviderError(overrun_code, research_cost_micro=getattr(exc, "research_cost_micro", None)) from None
            if isinstance(exc, UnsafeURL):
                raise ProviderError("research_request_rejected") from None
            if isinstance(exc, ProviderError) and exc.code == "research_rate_limited":
                raise ResearchDispatchError(exc.code, retryable=exc.retryable, retry_after=exc.retry_after,
                    outcome_unknown=exc.outcome_unknown, research_cost_micro=exc.research_cost_micro) from None
            raise
        micro = None
        if isinstance(result, ResearchResults):
            micro = result.cost_micro_usd
        elif isinstance(result, dict):
            micro = result.get("cost_micro_usd")
        cost = _cost(micro)
        accounting = await self.budget.reconcile(reservation, usage_tokens=0, actual_cost_usd=cost,
            outcome="succeeded" if cost is not None else "unknown", metadata=metadata)
        if cost is None:
            raise ProviderError("research_billing_unknown", outcome_unknown=True)
        if accounting.reservation_overrun:
            raise ProviderError(overrun_code, research_cost_micro=micro)
        return result

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        gateway = self.gateway
        if isinstance(gateway, TinyFishResearchGateway):
            return await self._dispatch("search", query, lambda: gateway.search(query, max_results, language=self.language))
        return await self._dispatch("search", query, lambda: gateway.search(query, max_results))

    async def extract(self, url: str) -> dict[str, Any]:
        return await self._dispatch("extract", url, lambda: self.gateway.extract(url), urls=1)

    async def extract_many(self, urls: tuple[str, ...]) -> list[dict[str, Any]]:
        gateway = self.gateway
        if not isinstance(gateway, TinyFishResearchGateway):
            raise ValueError("This provider does not support batch extraction")
        urls = tuple(canonicalize_url(url) for url in urls)
        cached: dict[str, dict[str, Any]] = {}
        previous: dict[str, dict[str, Any]] = {}
        if self.cache_seconds:
            rows = await self.budget.repository.recent_research_snapshots(
                self.claim, [source_identity(url) for url in urls],
            )
            now = datetime.now(UTC)
            for snapshot in rows:
                source, excerpt = snapshot["source"], snapshot["excerpt"]
                url = source.get("original_url")
                if url not in urls or url in previous:
                    continue
                text = excerpt.get("text", "")
                if (
                    not text or len(text) > gateway.max_chars_per_page
                    or source.get("content_hash") != excerpt.get("content_hash")
                    or markdown_hash(text) != excerpt.get("content_hash")
                ):
                    continue
                timestamp = source.get("revalidated_at") or source.get("retrieved_at")
                try:
                    age = now - datetime.fromisoformat(timestamp)
                except (TypeError, ValueError):
                    continue
                if not timedelta() <= age <= timedelta(days=1):
                    continue
                excerpt["flags"] = [flag for flag in excerpt.get("flags", []) if flag != "possible_numeric_conflict"]
                previous[url] = {**snapshot, "fixture": False, "cost_micro_usd": 0}
                if age <= timedelta(seconds=self.cache_seconds):
                    cached[url] = previous[url]
        missing = tuple(url for url in urls if url not in cached)
        if missing:
            prior = previous.get(missing[0]) if len(missing) == 1 else None
            fetched = await self._dispatch(
                "extract", "\n".join(missing), lambda: gateway.extract_many(missing, previous=prior), urls=len(missing),
            )
            cached.update((row["source"]["original_url"], row) for row in fetched)
        return [cached[url] for url in urls if url in cached]
