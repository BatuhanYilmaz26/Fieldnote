"""Supervised durable execution on the PostgreSQL session owning each lock.

The worker is the only component that creates paid provider requests. It
reserves capacity before every physical dispatch, classifies failures in one
place, persists retry waits instead of holding a worker slot, and resumes the
existing LangGraph thread instead of replaying the create payload.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import signal
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from fieldnote.domain import CreateRunRequest, EditorialReview
from fieldnote.domain.languages import language_profile
from fieldnote.domain.research import needs_fresh_sources
from fieldnote.graph import (
    GraphDependencies,
    GraphError,
    GraphHooks,
    build_graph,
    initial_state,
    make_fixture_dependencies,
)
from fieldnote.providers.base import ModelGateway, ProviderError
from fieldnote.providers.fixture import FixtureGateway, FixtureResearchGateway
from fieldnote.providers.openrouter import OpenRouterGateway
from fieldnote.providers.tavily import TavilyResearchGateway
from fieldnote.providers.tinyfish import TinyFishResearchGateway
from fieldnote.providers.treg import TregResearchGateway
from fieldnote.services.budgets import BudgetService, retry_decision
from fieldnote.services.citations import CitationError, render_citations
from fieldnote.services.research_accounting import AccountedResearch, ResearchDispatchError
from fieldnote.services.revision_review import normalize_review, review_input
from fieldnote.settings import Settings
from fieldnote.storage.pool import database_pool
from fieldnote.storage.repository import Claim, Repository, StorageError

logger = logging.getLogger("fieldnote.worker")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_MILLION = Decimal(1_000_000)


def estimate_input_tokens(messages: list[dict[str, str]]) -> int:
    """Conservative character-based estimate, never presented as exact usage."""
    raw = sum(len(message.get("content", "").encode("utf-8")) for message in messages)
    return max(1, math.ceil(raw / 3))


def _usage_tokens(usage: dict[str, Any] | None) -> int | None:
    if not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        isinstance(prompt, int)
        and not isinstance(prompt, bool)
        and prompt >= 0
        and isinstance(completion, int)
        and not isinstance(completion, bool)
        and completion >= 0
    ):
        return prompt + completion
    return None


def _deadline(claim: Claim) -> datetime:
    raw = claim.run.get("deadline_at")
    if isinstance(raw, datetime):
        value = raw
    else:
        value = datetime.fromisoformat(str(raw))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class AccountedGateway:
    """The single owner of durable reservation, repair and accounting."""

    def __init__(
        self,
        gateway: ModelGateway,
        budget: BudgetService,
        claim: Claim,
        settings: Settings,
    ) -> None:
        self.gateway = gateway
        self.budget = budget
        self.claim = claim
        self.settings = settings
        self.fixture = settings.model_provider == "fixture"

    def _estimated_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) * self.settings.openrouter_input_price_per_million
            + Decimal(output_tokens) * self.settings.openrouter_output_price_per_million
        ) / _MILLION

    async def _reserve(self, identity: str, total_tokens: int, amount: Decimal):
        return await self.budget.reserve_call(
            self.claim,
            identity,
            max_tokens=total_tokens,
            estimated_cost_usd=amount,
            fixture=self.fixture,
        )

    async def _dispatch(
        self,
        schema,
        messages: list[dict[str, str]],
        call_identity: str,
        reservation_identity: str,
        max_tokens: int,
    ):
        input_tokens = estimate_input_tokens(messages)
        if input_tokens > self.settings.model_max_input_tokens:
            raise ProviderError("model_input_too_large")
        total_tokens = input_tokens + max_tokens
        if total_tokens > 1_000_000:
            raise ProviderError("model_input_too_large")
        amount = self._estimated_cost(input_tokens, max_tokens)
        reservation = await self._reserve(reservation_identity, total_tokens, amount)
        if not reservation.dispatch_allowed:
            # A replayed identity may already have reached the provider. A
            # second dispatch would be duplicate spend and is never authorized.
            raise ProviderError("call_already_reserved", outcome_unknown=True)
        try:
            result = await self.gateway.generate(
                schema,
                messages,
                call_identity,
                max_tokens,
                reserved_cost_usd=amount if self.settings.model_policy == "budgeted" else None,
            )
        except BaseException as exc:
            response = exc.response if isinstance(exc, ProviderError) else None
            metadata = {"code": str(getattr(exc, "code", "dispatch_failed"))[:160]}
            if isinstance(exc, ProviderError):
                if exc.validation_kind:
                    metadata["validation_kind"] = exc.validation_kind
                if exc.schema_name:
                    metadata["schema_name"] = exc.schema_name
            if response:
                metadata.update({
                    "requested_model": response.requested_model,
                    "resolved_model": response.resolved_model or "unknown",
                    "provider": response.provider or "unknown",
                    **({"request_id": response.request_id} if response.request_id else {}),
                })
            accounting = await self.budget.reconcile(
                reservation,
                usage_tokens=_usage_tokens(response.usage) if response else None,
                actual_cost_usd=response.cost_usd if response else None,
                outcome=("unknown" if response is None and getattr(exc, "outcome_unknown", False)
                         else "failed"),
                retryable=bool(getattr(exc, "retryable", False)),
                metadata=metadata,
            )
            if (response and self.settings.model_policy == "free_only"
                    and response.cost_usd is not None and response.cost_usd > 0):
                raise ProviderError("provider_cost_policy_violation") from None
            if accounting.reservation_overrun:
                raise ProviderError("provider_cost_overrun") from None
            raise
        actual = result.cost_usd
        if (
            self.settings.model_policy == "free_only"
            and actual is not None
            and actual > 0
        ):
            await self.budget.reconcile(
                reservation,
                usage_tokens=_usage_tokens(result.usage),
                actual_cost_usd=actual,
                outcome="succeeded",
                metadata={
                    "requested_model": result.requested_model,
                    "resolved_model": result.resolved_model,
                    "provider": result.provider or "unknown",
                },
            )
            raise ProviderError("provider_cost_policy_violation", outcome_unknown=True)
        accounting = await self.budget.reconcile(
            reservation,
            usage_tokens=_usage_tokens(result.usage),
            actual_cost_usd=actual,
            outcome="succeeded",
            metadata={
                "requested_model": result.requested_model,
                "resolved_model": result.resolved_model,
                "provider": result.provider or "unknown",
                **({"request_id": result.request_id} if result.request_id else {}),
            },
        )
        if accounting.reservation_overrun:
            raise ProviderError("provider_cost_overrun")
        return result

    async def generate(
        self,
        schema,
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        **kwargs: Any,
    ):
        del kwargs
        identity = f"attempt:{self.claim.generation}:{call_identity}"
        try:
            return await self._dispatch(schema, messages, call_identity, identity, max_tokens)
        except ProviderError as exc:
            if (
                exc.code != "provider_schema_invalid"
                or self.settings.openrouter_schema_repairs < 1
            ):
                raise
            repair_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "The previous response did not match the required JSON schema. "
                        "Recheck the original instructions, including relationships between fields "
                        "such as exact totals and contiguous task order. "
                        "Return only one JSON object matching that schema. Do not add "
                        "commentary, links, tools or markdown fences."
                    ),
                },
            ]
            return await self._dispatch(
                schema,
                repair_messages,
                f"{call_identity}:repair1",
                f"{identity}:repair1",
                max_tokens,
            )


@dataclass(slots=True)
class ProviderRuntime:
    model: ModelGateway | None = None
    research: Any | None = None
    client: httpx.AsyncClient | None = None


@asynccontextmanager
async def provider_runtime(settings: Settings):
    """Reuse bounded HTTP clients for the whole worker lifetime."""
    external = settings.model_provider != "fixture" or settings.research_provider != "fixture"
    client = (
        httpx.AsyncClient(
            timeout=httpx.Timeout(settings.model_timeout_seconds, connect=5.0),
            limits=httpx.Limits(
                max_connections=max(4, settings.model_max_concurrency + 2),
                max_keepalive_connections=max(2, settings.model_max_concurrency),
            ),
            follow_redirects=False,
            trust_env=False,
        )
        if external
        else None
    )
    try:
        runtime = ProviderRuntime(client=client)
        if settings.model_provider == "openrouter":
            assert client is not None and settings.openrouter_api_key is not None
            runtime.model = OpenRouterGateway(
                client,
                settings.openrouter_api_key.get_secret_value(),
                settings.openrouter_model,
                settings.model_policy,
                allowed_models=settings.model_allowlist,
                allowed_providers=settings.openrouter_provider_allowlist,
            )
        if settings.research_provider == "treg":
            assert client is not None and settings.treg_token is not None
            runtime.research = TregResearchGateway(
                client,
                settings.treg_token.get_secret_value(),
                org=settings.treg_org,
                routing_policy=settings.treg_routing_policy,
                max_cost_micro=settings.treg_max_cost_micro,
                max_chars_per_page=settings.research_max_chars_per_page,
            )
        elif settings.research_provider == "tinyfish":
            assert client is not None and settings.tinyfish_api_key is not None
            runtime.research = TinyFishResearchGateway(
                client, settings.tinyfish_api_key.get_secret_value(), timeout=settings.research_timeout_seconds,
                max_chars_per_page=settings.research_max_chars_per_page,
            )
        elif settings.research_provider == "tavily":
            assert client is not None and settings.tavily_api_key is not None
            runtime.research = TavilyResearchGateway(
                client,
                settings.tavily_api_key.get_secret_value(),
                max_chars_per_page=settings.research_max_chars_per_page,
            )
        yield runtime
    finally:
        if client is not None:
            await client.aclose()


def _safe_message(code: str) -> str:
    if code == "review_interrupted":
        return "This review was interrupted after a model dispatch. Request a new review when you are ready; it was not repeated automatically."
    if code in {"review_revision_mismatch", "review_integrity_mismatch", "review_unknown_task"}:
        return "The assessment did not match the saved revision's review contract. Your text is unchanged and no assessment was applied."
    if code == "model_input_too_large":
        return "The draft and evidence exceed the configured model input limit. Shorten the content or review the model configuration before trying again."
    if code == "provider_capability_unavailable":
        return (
            "The configured AI model or writing capability is currently unavailable. "
            "Check model availability before starting a new draft."
        )
    if code == "provider_rate_limited":
        return (
            "The AI provider's request limit was reached. "
            "Wait for capacity to recover before starting a new draft."
        )
    if code == "provider_schema_invalid":
        return (
            "The AI response did not match the required writing format. "
            "No invalid content was accepted. Check the provider before starting a new draft."
        )
    if code in {"research_billing_unknown", "research_billing_unresolved", "research_route_cap_exceeded", "research_cost_policy_violation"}:
        return "Research stopped because a provider charge could not be verified against the configured limit. Check billing before retrying."
    if code == "research_price_unknown":
        return "The configured research adapter has no qualified billing contract. Select a qualified research provider or the local fixture profile."
    if code in {
        "provider_authentication_failed",
        "provider_policy_denied",
        "research_authentication_failed",
        "research_policy_denied",
        "provider_budget_exhausted",
        "research_budget_exhausted",
        "provider_price_unknown",
    }:
        return "Writing stopped because the configured provider rejected this run. Check provider policy and limits."
    if code in {"deadline_exceeded", "run_deadline_exceeded"}:
        return "The run deadline expired before execution completed."
    if code == "reviewer_unavailable":
        return "The editorial reviewer was unavailable. A partial draft is retained for review."
    return "This draft could not finish. Review the run or start again."


def _prepare_artifact(
    values: dict[str, Any], *, language: str = "en", failure_code: str | None = None,
) -> dict[str, Any] | None:
    raw = values.get("assembled_markdown")
    if not isinstance(raw, str) or not raw.strip():
        return None
    sources = values.get("sources", {}) if isinstance(values.get("sources"), dict) else {}
    try:
        markdown = render_citations(raw, sources, language=language)
    except CitationError:
        markdown = raw
    decision: dict[str, Any] = (
        values["decision"] if isinstance(values.get("decision"), dict) else {}
    )
    profile = language_profile(language)
    if values.get("fixture_calls", 0):
        markdown = f"> {profile.synthetic_notice}\n\n" + markdown
    elif not decision.get("need_research", False):
        markdown = f"> {profile.unresearched_notice}\n\n" + markdown
    issues = [item for item in values.get("issues", []) if isinstance(item, dict)]
    if failure_code and not any(item.get("code") == failure_code for item in issues):
        issues.append(
            {
                "code": failure_code,
                "severity": "error",
                "explanation": "Generation stopped before an approved revision existed. This partial artifact is retained for review only.",
            }
        )
    claims = [item for item in values.get("claims", []) if isinstance(item, dict)]
    citation_map = {
        "language": language,
        "sources": sources,
        "editable_markdown": raw,
        "research_limitations": values.get("research_limitations", []),
        "headline_options": values.get("plan", {}).get("headline_options", []),
    }
    return {
        "markdown": markdown,
        "issues": issues,
        "claims": claims,
        "citation_map": citation_map,
    }


def _dependencies(
    settings: Settings,
    request: CreateRunRequest,
    claim: Claim,
    runtime: ProviderRuntime,
    budget: BudgetService,
    hooks: GraphHooks,
    model_calls: int,
) -> GraphDependencies:
    base = make_fixture_dependencies(
        request,
        hooks=hooks,
        max_review_rounds=settings.max_review_rounds,
        max_model_calls=settings.max_model_calls,
    )
    model = runtime.model if runtime.model is not None else base.model
    research = runtime.research if runtime.research is not None else base.research
    if settings.research_provider == "fixture" and not isinstance(research, FixtureResearchGateway):
        research = base.research
    if settings.research_provider != "fixture":
        if research is None:
            raise ProviderError("research_not_configured")
        direct = settings.research_provider == "tinyfish"
        rpm = min(settings.research_provider_rpm, 30) if direct else settings.research_provider_rpm
        daily = min(settings.research_provider_daily_requests, 500) if direct else settings.research_provider_daily_requests
        urls = min(settings.research_provider_daily_urls, 1000) if direct else settings.research_provider_daily_urls
        research_budget = BudgetService(budget.repository, call_kind="research",
            max_run_calls=settings.max_research_calls, user_daily_calls=settings.research_user_daily_calls,
            provider_rpm=rpm, provider_tpm=1,
            provider_daily_requests=daily,
            provider_daily_urls=urls,
            run_budget_usd=settings.run_budget_usd, user_daily_budget_usd=settings.user_daily_budget_usd,
            provider_scope=f"research/{settings.research_provider}",
            circuit_failures=settings.provider_circuit_failures, circuit_seconds=settings.provider_circuit_seconds)
        cache_seconds = (
            settings.tinyfish_cache_seconds if direct and not needs_fresh_sources(request.topic, request.research_context())
            else 0
        )
        research = AccountedResearch(
            research, research_budget, claim, settings, language=request.language, cache_seconds=cache_seconds,
        )
    return replace(
        base,
        model=AccountedGateway(model, budget, claim, settings),
        research=research,
        max_concurrency=settings.model_max_concurrency,
        initial_model_calls=min(model_calls, settings.max_model_calls),
        max_output_tokens=settings.model_max_output_tokens,
        max_research_queries=settings.max_research_queries,
        max_results_per_query=settings.max_results_per_query,
        max_extracted_pages=settings.max_extracted_pages,
        max_evidence_tokens=settings.max_evidence_tokens,
    )


async def _finalize(
    repository: Repository,
    claim: Claim,
    values: dict[str, Any],
    *,
    failure_code: str | None = None,
) -> dict[str, Any] | None:
    prepared = _prepare_artifact(
        values, language=claim.run["request"].get("language", "en"), failure_code=failure_code,
    )
    if prepared is None:
        return None
    if failure_code:
        await repository.finalize(
            claim,
            prepared["markdown"],
            review_status="failed",
            issues=prepared["issues"],
            citation_map=prepared["citation_map"],
            claims=prepared["claims"],
            event_type="failed",
            error_code=failure_code,
        )
    else:
        status = str(values.get("review_status") or "needs_review")
        if status not in {"approved", "needs_review"}:
            status = "needs_review"
        await repository.finalize(
            claim,
            prepared["markdown"],
            review_status=status,
            issues=prepared["issues"],
            citation_map=prepared["citation_map"],
            claims=prepared["claims"],
        )
    return prepared


async def execute_claim(
    repository: Repository,
    claim: Claim,
    settings: Settings,
    runtime: ProviderRuntime | None = None,
) -> dict[str, Any]:
    """Run or resume one thread, then commit exactly one artifact transaction."""
    request = CreateRunRequest.model_validate(claim.run["request"])
    revision_review = claim.run.get("kind") == "revision_review"
    if revision_review:
        settings = settings.model_copy(update={
            "max_model_calls": min(2, settings.max_model_calls), "max_job_attempts": 1,
        })
    budget = BudgetService(
        repository,
        max_run_calls=settings.max_model_calls,
        user_daily_calls=settings.user_daily_calls,
        provider_rpm=settings.provider_rpm,
        provider_tpm=settings.provider_tpm,
        run_budget_usd=settings.run_budget_usd,
        user_daily_budget_usd=settings.user_daily_budget_usd,
        provider_scope=settings.model_provider,
        circuit_failures=settings.provider_circuit_failures,
        circuit_seconds=settings.provider_circuit_seconds,
    )

    async def on_policy(policy: dict[str, Any]) -> None:
        await repository.save_policy(claim, policy)

    async def on_research(limitations: list[str], source_count: int) -> None:
        await repository.save_research(claim, limitations, source_count)

    async def on_plan(plan: dict[str, Any]) -> None:
        await repository.save_plan(claim, plan)

    async def on_source(source: dict[str, Any], excerpt: dict[str, Any]) -> None:
        await repository.save_sources(claim, [source], [excerpt])

    async def on_section(section: dict[str, Any]) -> None:
        await repository.save_section(claim, section["task_id"], section["revision"], section)

    async def check_active() -> None:
        await repository.check_active(claim)

    hooks = GraphHooks(
        on_policy=on_policy,
        on_research=on_research,
        on_plan=on_plan,
        on_source=on_source,
        on_section=on_section,
        check_active=check_active,
        before_model_call=lambda _identity: repository.check_active(claim),
    )

    owns_runtime = runtime is None
    if runtime is None:
        runtime_context = provider_runtime(settings)
        runtime = await runtime_context.__aenter__()
    else:
        runtime_context = None
    graph: Any = None
    config: RunnableConfig = {}
    try:
        if revision_review:
            # A reclaimed job with an already dispatched request must not
            # silently repeat a review whose remote outcome may be unknown.
            if int(claim.run.get("model_call_count", 0)):
                raise ProviderError("review_interrupted")
            subject = await repository.review_subject(claim)
            messages, issues, claims = review_input(subject)
            gateway = runtime.model or FixtureGateway({"revision-review": {
                "approved": False, "assessed_revision": 0,
                "assessed_hash": subject["article"]["content_hash"], "issues": [],
            }})
            accounted = AccountedGateway(gateway, budget, claim, settings)
            remaining = (_deadline(claim) - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise ProviderError("deadline_exceeded")
            try:
                async with asyncio.timeout(remaining):
                    result = await accounted.generate(
                        EditorialReview, messages, "revision-review",
                        max_tokens=settings.model_max_output_tokens,
                    )
                    review = normalize_review(result, subject, issues, fixture=settings.model_provider == "fixture")
                    await repository.finalize_review(claim, review, claims)
            except TimeoutError:
                raise ProviderError("deadline_exceeded") from None
            return {"outcome": "succeeded" if review["approved"] else "needs_review"}
        deps = _dependencies(
            settings,
            request,
            claim,
            runtime,
            budget,
            hooks,
            int(claim.run.get("model_call_count", 0)),
        )
        saver = AsyncPostgresSaver(
            claim.connection,
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_msgpack_modules=None,
                allowed_json_modules=None,
            ),
        )
        graph = build_graph(deps).compile(checkpointer=saver)
        config = {
            "configurable": {"thread_id": claim.run_id},
            "recursion_limit": 80,
        }
        snapshot = await graph.aget_state(config)
        if snapshot.values and not snapshot.next and snapshot.values.get("final_markdown"):
            output = dict(snapshot.values)
        else:
            seed = None if snapshot.values else initial_state(claim.run_id)
            remaining = (_deadline(claim) - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise StorageError("deadline_exceeded", "The run deadline has expired")
            try:
                async with asyncio.timeout(remaining):
                    output = await graph.ainvoke(seed, config=config, durability="sync")
            except TimeoutError:
                return await _handle_failure(
                    repository,
                    claim,
                    settings,
                    graph,
                    config,
                    ProviderError("deadline_exceeded"),
                )
        if not output or "final_markdown" not in output:
            output = dict((await graph.aget_state(config)).values)
        prepared = await _finalize(repository, claim, dict(output))
        if prepared is None:
            raise StorageError("invalid_article", "The workflow produced no article", 422)
        return {
            "outcome": output.get("outcome", "needs_review"),
            "review_status": output.get("review_status"),
            "revision_id": None,
        }
    except (StorageError, asyncio.CancelledError):
        raise
    except BaseException as exc:
        return await _handle_failure(
            repository, claim, settings, graph, config, exc
        )
    finally:
        if owns_runtime and runtime_context is not None:
            await runtime_context.__aexit__(None, None, None)


async def _handle_failure(
    repository: Repository,
    claim: Claim,
    settings: Settings,
    graph: Any,
    config: RunnableConfig,
    exc: BaseException,
) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
        code = "generation_failed"
    if isinstance(exc, ProviderError) and exc.retryable:
        run = await repository.get_run(claim.run["owner_id"], claim.run_id)
        if isinstance(exc, ResearchDispatchError):
            remaining_calls = max(0, settings.max_research_calls - run["research_call_count"])
        else:
            remaining_calls = max(0, settings.max_model_calls - run["model_call_count"])
        decision = retry_decision(
            exc,
            attempt=int(claim.run.get("attempt", 1)),
            deadline=_deadline(claim),
            remaining_calls=remaining_calls,
            max_attempts=settings.max_job_attempts,
        )
        if decision.retry:
            try:
                await repository.retry_job(claim, code, decision.delay_seconds)
                logger.info(
                    "run_retry_wait run_id=%s code=%s delay=%s",
                    claim.run_id,
                    code,
                    round(decision.delay_seconds, 1),
                )
                return {"outcome": "retry_wait", "error_code": code}
            except StorageError:
                # Cancellation or lease loss wins over a retry request.
                pass
    values: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        values = dict((await graph.aget_state(config)).values)
    if values.get("assembled_markdown"):
        with contextlib.suppress(StorageError):
            await _finalize(repository, claim, values, failure_code=code)
            logger.info("run_failed_with_partial run_id=%s code=%s", claim.run_id, code)
            return {"outcome": "failed", "error_code": code, "partial": True}
    await repository.fail_job(claim, code, _safe_message(code))
    logger.info("run_failed run_id=%s code=%s", claim.run_id, code)
    return {"outcome": "failed", "error_code": code}


async def process_claim(
    repository: Repository,
    claim: Claim,
    settings: Settings,
    runtime: ProviderRuntime | None = None,
) -> None:
    task = asyncio.create_task(execute_claim(repository, claim, settings, runtime))

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.job_heartbeat_seconds)
            if not await repository.heartbeat(claim):
                task.cancel()
                return

    keeper = asyncio.create_task(heartbeat())
    try:
        await task
    except asyncio.CancelledError:
        # A worker drain requeues the same thread; a user cancellation turns
        # into a terminal cancelled state. Never publish late generated work.
        try:
            await repository.retry_job(claim, "worker_shutdown", 0)
        except StorageError:
            with contextlib.suppress(StorageError):
                await repository.fail_job(
                    claim,
                    "execution_cancelled",
                    "Execution stopped. Remaining remote costs may be unknown.",
                )
        raise
    except (GraphError, ProviderError, StorageError) as exc:
        code = getattr(exc, "code", "generation_failed")
        with contextlib.suppress(StorageError):
            await repository.fail_job(claim, code, _safe_message(str(code)))
    except Exception as exc:
        logger.error("run_failed run_id=%s exception_type=%s", claim.run_id, type(exc).__name__)
        with contextlib.suppress(StorageError):
            await repository.fail_job(
                claim, "generation_failed", "This draft could not finish. Try again later."
            )
    finally:
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keeper
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await claim.close()


async def serve_worker(
    settings: Settings,
    *,
    once: bool = False,
    stop: asyncio.Event | None = None,
    runtime: ProviderRuntime | None = None,
) -> None:
    """Run the supervised worker loop.

    ``stop`` and ``runtime`` are injectable so acceptance tests can drain an
    active claim and inject a bounded slow gateway without process signals.
    """
    if settings.database_url is None:
        raise RuntimeError("Set DATABASE_URL and run migrations before starting the worker.")
    dsn = settings.database_url.get_secret_value()
    worker_id = f"worker-{uuid4()}"
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    runtime_scope = provider_runtime(settings) if runtime is None else contextlib.nullcontext(runtime)
    async with database_pool(
        dsn,
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        timeout=settings.database_pool_timeout,
    ) as pool, runtime_scope as active_runtime:
        repository = Repository(
            pool,
            dsn,
            max_user_runs=settings.max_user_nonterminal_runs,
            max_global_runs=settings.max_global_nonterminal_runs,
            max_run_seconds=settings.max_run_seconds,
            graph_version=settings.graph_version,
        )
        active: dict[asyncio.Task[Any], Claim] = {}

        async def registry() -> None:
            while not stop.is_set():
                try:
                    first = next(iter(active.values()), None)
                    await repository.worker_heartbeat(
                        worker_id,
                        draining=stop.is_set(),
                        active_run_id=first.run_id if first else None,
                        active_count=len(active),
                    )
                except Exception:
                    logger.warning("worker_registry_write_failed")
                await asyncio.sleep(
                    min(settings.job_heartbeat_seconds, settings.worker_stale_seconds / 3)
                )

        await repository.worker_heartbeat(worker_id)
        registry_task = asyncio.create_task(registry())
        stop_task = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    await repository.expire_waiting()
                    await repository.activate_due_retries()
                while len(active) < settings.worker_concurrency and not stop.is_set():
                    claim = await repository.claim_job(
                        worker_id, lease_seconds=settings.job_lease_seconds
                    )
                    if claim is None:
                        break
                    task = asyncio.create_task(
                        process_claim(repository, claim, settings, active_runtime)
                    )
                    active[task] = claim
                if once:
                    if active:
                        await asyncio.gather(*active, return_exceptions=True)
                    return
                waiters: set[asyncio.Task[Any]] = set(active)
                waiters.add(stop_task)
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=settings.scheduler_poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    if task is stop_task:
                        continue
                    active.pop(task, None)
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        logger.error(
                            "worker_task_failed exception_type=%s", type(error).__name__
                        )
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await repository.worker_heartbeat(worker_id, draining=True, active_count=len(active))
            if active:
                _, pending = await asyncio.wait(
                    set(active), timeout=settings.worker_drain_seconds
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            registry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await registry_task
            with contextlib.suppress(Exception):
                await repository.worker_heartbeat(worker_id, draining=True, active_count=0)
