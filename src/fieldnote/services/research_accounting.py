"""One durable reservation per outbound research attempt, with no hidden retry."""

import hashlib
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from fieldnote.providers.base import DispatchAdmissionError, ProviderError, ResearchResults
from fieldnote.providers.tinyfish import TinyFishResearchGateway
from fieldnote.services.budgets import BudgetError, BudgetService
from fieldnote.services.research import ResearchGateway
from fieldnote.services.urls import UnsafeURL
from fieldnote.settings import Settings
from fieldnote.storage.repository import Claim


class ResearchDispatchError(ProviderError, DispatchAdmissionError):
    """A request failure that must release the job to its single retry owner."""


def _cost(micro: Any) -> Decimal | None:
    if type(micro) is not int or not 0 <= micro <= 1_000_000_000:
        return None
    return Decimal(micro) / Decimal(1_000_000)


class AccountedResearch:
    def __init__(self, gateway: ResearchGateway, budget: BudgetService, claim: Claim, settings: Settings,
                 *, language: str | None = None) -> None:
        self.gateway = gateway
        self.budget = budget
        self.claim = claim
        self.settings = settings
        self.sequence = 0
        self.language = language
        self.batch_size = gateway.batch_size if isinstance(gateway, TinyFishResearchGateway) else 1

    async def _dispatch[T](self, operation: str, target: str, call: Callable[[], Awaitable[T]], *, urls: int = 0) -> T:
        # A route cap is the qualified conservative monetary bound, not an
        # assumed average price. Unpriced adapters do not inherit zero cost.
        provider = self.settings.research_provider
        if provider not in {"treg", "tinyfish"}:
            raise BudgetError("research_price_unknown")
        cap = self.settings.treg_max_cost_micro if provider == "treg" else 0
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
                code = "research_cost_policy_violation" if self.settings.treg_routing_policy == "free_only" else "research_route_cap_exceeded"
                raise ProviderError(code, research_cost_micro=getattr(exc, "research_cost_micro", None)) from None
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
            code = "research_cost_policy_violation" if self.settings.treg_routing_policy == "free_only" else "research_route_cap_exceeded"
            raise ProviderError(code, research_cost_micro=micro)
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
        return await self._dispatch("extract", "\n".join(urls), lambda: gateway.extract_many(urls), urls=len(urls))
