"""Shared PostgreSQL dispatch admission and conservative usage accounting.

The committed reservation is the dispatch barrier. A duplicate authorization
never permits another remote request. A later retry needs a new attempt identity
and consumes every quota again. Accounting remains possible after cancellation
or lease loss, because recording a bill must not authorize more generated work.
"""

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from fieldnote.providers.base import DispatchAdmissionError, ProviderError
from fieldnote.storage.repository import Claim, Repository

BUDGET_LOCK = 1195461971
_IDENTITY = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_MONEY_UNIT = Decimal("0.00000001")
_MAX_MONEY = Decimal("1000000000")
_SAFE_METADATA = frozenset({
    "requested_model", "resolved_model", "provider", "request_id", "code",
    "validation_kind", "schema_name", "operation",
})


class BudgetError(ProviderError, DispatchAdmissionError):
    """A public-safe admission failure with a scheduler-readable retry hint."""


@dataclass(frozen=True, slots=True)
class CallReservation:
    run_id: str
    call_identity: str
    attempt: int
    dispatch_allowed: bool
    amount_usd: Decimal
    reserved_tokens: int
    provider_scope: str


@dataclass(frozen=True, slots=True)
class AccountingResult:
    reservation_status: Literal["reserved", "reconciled", "unknown"]
    cost_known: bool
    tokens_known: bool
    reservation_overrun: bool


def _money(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value >= _MAX_MONEY:
        raise ValueError(f"{name} must be a finite nonnegative Decimal below {_MAX_MONEY}")
    # Never round a positive estimate down; callers choose a conservative bound.
    return value.quantize(_MONEY_UNIT, rounding="ROUND_CEILING")


class BudgetService:
    def __init__(
        self,
        repository: Repository,
        *,
        max_run_calls: int = 32,
        user_daily_calls: int = 100,
        provider_rpm: int = 10,
        provider_tpm: int = 100_000,
        run_budget_usd: Decimal = Decimal("0"),
        user_daily_budget_usd: Decimal = Decimal("0"),
        provider_scope: str = "openrouter",
        circuit_failures: int = 3,
        circuit_seconds: int = 30,
        call_kind: Literal["model", "research"] = "model",
        provider_daily_requests: int = 100,
        provider_daily_urls: int = 100,
    ) -> None:
        for name, value in {
            "max_run_calls": max_run_calls,
            "user_daily_calls": user_daily_calls,
            "provider_rpm": provider_rpm,
            "provider_tpm": provider_tpm,
            "circuit_failures": circuit_failures,
            "circuit_seconds": circuit_seconds,
            "provider_daily_requests": provider_daily_requests,
            "provider_daily_urls": provider_daily_urls,
        }.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not _IDENTITY.fullmatch(provider_scope):
            raise ValueError("Invalid provider quota scope")
        self.repository = repository
        self.max_run_calls = max_run_calls
        self.user_daily_calls = user_daily_calls
        self.provider_rpm = provider_rpm
        self.provider_tpm = provider_tpm
        self.run_budget_usd = _money(run_budget_usd, "run_budget_usd")
        self.user_daily_budget_usd = _money(user_daily_budget_usd, "user_daily_budget_usd")
        self.provider_scope = provider_scope
        self.circuit_failures = circuit_failures
        self.circuit_seconds = circuit_seconds
        if call_kind not in {"model", "research"}:
            raise ValueError("Invalid provider call kind")
        self.call_kind = call_kind
        self.provider_daily_requests = provider_daily_requests
        self.provider_daily_urls = provider_daily_urls

    async def reserve_call(
        self,
        claim: Claim,
        call_identity: str,
        *,
        max_tokens: int,
        estimated_cost_usd: Decimal | None,
        fixture: bool = False,
        url_count: int = 0,
    ) -> CallReservation:
        """Reserve one bounded outbound attempt under the active execution fence.

        ``max_tokens`` is the caller's conservative total input-plus-output
        bound, not merely the model's output-token setting. ``None`` price is
        unknown and denied, except explicitly synthetic fixture calls.
        """
        if not _IDENTITY.fullmatch(call_identity):
            raise ValueError("Invalid dispatch identity")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not (1 if self.call_kind == "model" else 0) <= max_tokens <= 1_000_000
        ):
            raise ValueError("A bounded positive total token estimate is required")
        if type(url_count) is not int or not 0 <= url_count <= 8 or (self.call_kind == "model" and url_count):
            raise ValueError("Invalid research URL reservation")
        if self.call_kind == "research" and max_tokens:
            raise ValueError("Research requests cannot reserve model tokens")
        if not fixture and max_tokens > self.provider_tpm:
            raise BudgetError("provider_token_limit_exceeded")
        if estimated_cost_usd is None:
            if not fixture:
                raise BudgetError("provider_price_unknown")
            amount = Decimal("0")
        else:
            amount = _money(estimated_cost_usd, "estimated_cost_usd")
        if fixture and amount:
            raise ValueError("Synthetic fixture calls cannot reserve paid spend")
        async with self.repository.pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_LOCK,))
            run = await self.repository._fence(connection, claim)
            existing = await (
                await connection.execute(
                    "SELECT p.attempt,p.reserved_tokens,p.provider_scope,p.call_kind,p.reserved_urls,b.amount_usd FROM provider_calls p "
                    "JOIN budget_reservations b ON b.run_id=p.run_id AND b.call_id=p.call_id "
                    "WHERE p.run_id=%s AND p.call_id=%s",
                    (UUID(claim.run_id), call_identity),
                )
            ).fetchone()
            if existing:
                if (
                    existing["provider_scope"] != self.provider_scope
                    or existing["reserved_tokens"] != max_tokens
                    or existing["amount_usd"] != amount
                    or existing["call_kind"] != self.call_kind
                    or existing["reserved_urls"] != url_count
                ):
                    raise BudgetError("call_identity_conflict")
                return CallReservation(
                    claim.run_id,
                    call_identity,
                    existing["attempt"],
                    False,
                    existing["amount_usd"],
                    existing["reserved_tokens"],
                    self.provider_scope,
                )
            counter = "model_call_count" if self.call_kind == "model" else "research_call_count"
            if run[counter] >= self.max_run_calls:
                raise BudgetError("run_call_limit_exhausted")
            count = await (
                await connection.execute(
                    "SELECT count(*) AS calls FROM provider_calls p JOIN runs r ON r.id=p.run_id "
                    "WHERE r.owner_id=%s AND p.call_kind=%s AND p.created_at>=date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
                    (run["owner_id"], self.call_kind),
                )
            ).fetchone()
            if count is None:
                raise BudgetError("budget_store_unavailable", retryable=True)
            if count["calls"] >= self.user_daily_calls:
                raise BudgetError("user_daily_call_limit_exhausted")
            if not fixture:
                if self.call_kind == "research":
                    daily = await (await connection.execute(
                        "WITH calls AS (SELECT p.*,b.actual_usd,j.lease_owner,j.lease_expires_at,j.generation,"
                        "p.created_at>=date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS today "
                        "FROM provider_calls p JOIN budget_reservations b ON b.run_id=p.run_id AND b.call_id=p.call_id "
                        "JOIN jobs j ON j.run_id=p.run_id WHERE p.provider_scope=%s AND p.call_kind='research') "
                        "SELECT count(*) FILTER (WHERE today) AS requests,coalesce(sum(reserved_urls) FILTER (WHERE today),0) AS urls,"
                        "bool_or((actual_usd IS NULL AND (status<>'reserved' OR lease_owner IS NULL OR "
                        "lease_expires_at<=clock_timestamp() OR fencing_generation<>generation)) OR "
                        "coalesce((metadata->>'reservation_overrun')::boolean,false)) AS billing_hold FROM calls",
                        (self.provider_scope,),
                    )).fetchone()
                    if daily is None:
                        raise BudgetError("budget_store_unavailable", retryable=True)
                    if daily["billing_hold"]:
                        raise BudgetError("research_billing_unresolved")
                    if daily["requests"] >= self.provider_daily_requests or daily["urls"] + url_count > self.provider_daily_urls:
                        raise BudgetError("research_daily_quota_exhausted")
                circuit = await (
                    await connection.execute(
                        "SELECT blocked_until,blocked_until>clock_timestamp() AS blocked,"
                        "extract(epoch from blocked_until-clock_timestamp()) AS wait FROM provider_circuits WHERE provider_scope=%s",
                        (self.provider_scope,),
                    )
                ).fetchone()
                if circuit and circuit["blocked"]:
                    raise BudgetError(
                        "provider_circuit_open", retryable=True, retry_after=float(circuit["wait"])
                    )
                quota = await (
                    await connection.execute(
                        "SELECT count(*) AS requests,coalesce(sum(coalesce(actual_tokens,reserved_tokens)),0) AS tokens,"
                        "coalesce(extract(epoch from min(created_at)+interval '60 seconds'-clock_timestamp()),1) AS wait "
                        "FROM provider_calls WHERE provider_scope=%s AND created_at>clock_timestamp()-interval '60 seconds'",
                        (self.provider_scope,),
                    )
                ).fetchone()
                if quota is None:
                    raise BudgetError("budget_store_unavailable", retryable=True)
                if (
                    quota["requests"] >= self.provider_rpm
                    or quota["tokens"] + max_tokens > self.provider_tpm
                ):
                    raise BudgetError(
                        "provider_quota_wait",
                        retryable=True,
                        retry_after=max(1.0, float(quota["wait"])),
                    )
            spend = await (
                await connection.execute(
                    "SELECT coalesce(sum(coalesce(b.actual_usd,b.amount_usd)) FILTER (WHERE b.run_id=%s),0) AS run_spend,"
                    "coalesce(sum(coalesce(b.actual_usd,b.amount_usd)) FILTER (WHERE r.owner_id=%s AND "
                    "b.created_at>=date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),0) AS user_spend "
                    "FROM budget_reservations b JOIN runs r ON r.id=b.run_id "
                    "WHERE b.run_id=%s OR (r.owner_id=%s AND b.created_at>="
                    "date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')",
                    (UUID(claim.run_id), run["owner_id"], UUID(claim.run_id), run["owner_id"]),
                )
            ).fetchone()
            if spend is None:
                raise BudgetError("budget_store_unavailable", retryable=True)
            if spend["run_spend"] + amount > self.run_budget_usd:
                raise BudgetError("run_budget_exhausted")
            if spend["user_spend"] + amount > self.user_daily_budget_usd:
                raise BudgetError("user_daily_budget_exhausted")
            attempt = run[counter] + 1
            await connection.execute(
                f"UPDATE runs SET {counter}=%s WHERE id=%s", (attempt, UUID(claim.run_id))
            )
            await connection.execute(
                "INSERT INTO provider_calls(run_id,call_id,attempt,status,metadata,provider_scope,reserved_tokens,fencing_generation,call_kind,reserved_urls) "
                "VALUES (%s,%s,%s,'reserved',%s,%s,%s,%s,%s,%s)",
                (
                    UUID(claim.run_id),
                    call_identity,
                    attempt,
                    Jsonb({"fixture": fixture}),
                    self.provider_scope,
                    max_tokens,
                    claim.generation,
                    self.call_kind,
                    url_count,
                ),
            )
            await connection.execute(
                "INSERT INTO budget_reservations(run_id,call_id,amount_usd,status) VALUES (%s,%s,%s,'reserved')",
                (UUID(claim.run_id), call_identity, amount),
            )
            return CallReservation(
                claim.run_id, call_identity, attempt, True, amount, max_tokens, self.provider_scope
            )

    async def reconcile(
        self,
        reservation: CallReservation,
        *,
        usage_tokens: int | None = None,
        actual_cost_usd: Decimal | None = None,
        outcome: Literal["succeeded", "failed", "unknown"] = "unknown",
        metadata: dict[str, Any] | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> AccountingResult:
        """Record usage even after cancellation/lease loss; never authorize work.

        Unknown amounts remain conservatively reserved. Once a charge/token
        count is known, later reconciliation cannot silently replace it.
        """
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Invalid call outcome")
        if usage_tokens is not None and (
            not isinstance(usage_tokens, int)
            or isinstance(usage_tokens, bool)
            or usage_tokens < 0
            or usage_tokens > 10_000_000
        ):
            raise ValueError("Invalid actual token count")
        cost = _money(actual_cost_usd, "actual_cost_usd") if actual_cost_usd is not None else None
        safe = {
            key: value[:160]
            for key, value in (metadata or {}).items()
            if key in _SAFE_METADATA and isinstance(value, str)
        }
        async with self.repository.pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_LOCK,))
            row = await (
                await connection.execute(
                    "SELECT p.status AS call_status,p.metadata,p.actual_tokens,p.reserved_tokens,p.provider_scope,"
                    "b.amount_usd,b.actual_usd,b.status AS reservation_status "
                    "FROM provider_calls p JOIN budget_reservations b ON b.run_id=p.run_id AND b.call_id=p.call_id "
                    "WHERE p.run_id=%s AND p.call_id=%s AND p.attempt=%s FOR UPDATE OF p,b",
                    (UUID(reservation.run_id), reservation.call_identity, reservation.attempt),
                )
            ).fetchone()
            if row is None or row["provider_scope"] != reservation.provider_scope:
                raise BudgetError("reservation_not_found")
            if row["actual_usd"] is not None and cost is not None and row["actual_usd"] != cost:
                raise BudgetError("accounting_conflict")
            if (
                row["actual_tokens"] is not None
                and usage_tokens is not None
                and row["actual_tokens"] != usage_tokens
            ):
                raise BudgetError("accounting_conflict")
            known_cost = row["actual_usd"] if cost is None else cost
            known_tokens = row["actual_tokens"] if usage_tokens is None else usage_tokens
            overrun = (known_cost is not None and known_cost > row["amount_usd"]) or (
                known_tokens is not None and known_tokens > row["reserved_tokens"]
            )
            status: Literal["reserved", "reconciled", "unknown"] = (
                "reconciled" if known_cost is not None else "unknown"
            )
            previous_outcome = row["call_status"]
            if previous_outcome in {"succeeded", "failed"} and outcome not in {
                previous_outcome,
                "unknown",
            }:
                raise BudgetError("accounting_conflict")
            final_outcome = (
                previous_outcome
                if outcome == "unknown" and previous_outcome in {"succeeded", "failed"}
                else outcome
            )
            combined = dict(row["metadata"])
            combined.update(safe)
            combined["reservation_overrun"] = bool(overrun)
            await connection.execute(
                "UPDATE provider_calls SET status=%s,actual_tokens=%s,metadata=%s WHERE run_id=%s AND call_id=%s AND attempt=%s",
                (
                    final_outcome,
                    known_tokens,
                    Jsonb(combined),
                    UUID(reservation.run_id),
                    reservation.call_identity,
                    reservation.attempt,
                ),
            )
            await connection.execute(
                "UPDATE budget_reservations SET actual_usd=%s,status=%s WHERE run_id=%s AND call_id=%s",
                (known_cost, status, UUID(reservation.run_id), reservation.call_identity),
            )
            # Repeated reconciliation must not multiply breaker failures.
            if not combined.get("fixture"):
                if outcome == "succeeded" and previous_outcome != "succeeded":
                    await connection.execute(
                        "INSERT INTO provider_circuits(provider_scope) VALUES (%s) ON CONFLICT(provider_scope) "
                        "DO UPDATE SET consecutive_failures=0,blocked_until=NULL,updated_at=clock_timestamp() "
                        "WHERE NOT %s OR provider_circuits.blocked_until IS NULL OR "
                        "provider_circuits.blocked_until<=clock_timestamp()",
                        (reservation.provider_scope, self.call_kind == "research"),
                    )
                elif retryable and previous_outcome == "reserved":
                    await connection.execute(
                        "INSERT INTO provider_circuits(provider_scope,consecutive_failures,blocked_until) "
                        "VALUES (%s,1,CASE WHEN %s<=1 THEN clock_timestamp()+(%s*interval '1 second') ELSE NULL END) "
                        "ON CONFLICT(provider_scope) DO UPDATE SET consecutive_failures=provider_circuits.consecutive_failures+1,"
                        "blocked_until=CASE WHEN provider_circuits.consecutive_failures+1>=%s "
                        "THEN greatest(provider_circuits.blocked_until,clock_timestamp()+(%s*interval '1 second')) "
                        "ELSE provider_circuits.blocked_until END,updated_at=clock_timestamp()",
                        (
                            reservation.provider_scope,
                            self.circuit_failures,
                            self.circuit_seconds,
                            self.circuit_failures,
                            self.circuit_seconds,
                        ),
                    )
                if self.call_kind == "research" and previous_outcome == "reserved" and retry_after is not None and math.isfinite(retry_after) and retry_after > 0:
                    await connection.execute(
                        "INSERT INTO provider_circuits(provider_scope,blocked_until) "
                        "VALUES (%s,clock_timestamp()+(%s*interval '1 second')) ON CONFLICT(provider_scope) "
                        "DO UPDATE SET blocked_until=greatest(provider_circuits.blocked_until,excluded.blocked_until),"
                        "updated_at=clock_timestamp()",
                        (reservation.provider_scope, min(retry_after, 31_536_000)),
                    )
            return AccountingResult(
                status, known_cost is not None, known_tokens is not None, bool(overrun)
            )


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    delay_seconds: float = 0.0
    reason: str = "permanent_failure"


def retry_decision(
    error: ProviderError,
    *,
    attempt: int,
    deadline: datetime,
    now: datetime | None = None,
    remaining_calls: int,
    max_attempts: int = 3,
    jitter: float = 0.5,
) -> RetryDecision:
    """One scheduler retry policy; no sleeps or outbound requests occur here."""
    if attempt < 1 or max_attempts < 1 or not 0 <= jitter <= 1:
        raise ValueError("Invalid retry policy inputs")
    if deadline.tzinfo is None or (now is not None and now.tzinfo is None):
        raise ValueError("Retry deadlines must be timezone-aware")
    if not error.retryable:
        return RetryDecision(False)
    if attempt >= max_attempts or remaining_calls <= 0:
        return RetryDecision(False, reason="attempt_budget_exhausted")
    remaining = (deadline - (now or datetime.now(UTC))).total_seconds()
    base = min(30.0, 0.5 * 2 ** min(attempt - 1, 8)) * (0.5 + jitter)
    retry_after = error.retry_after
    if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
        retry_after = None
    delay = max(base, retry_after or 0.0)
    if delay >= remaining:
        return RetryDecision(False, reason="deadline_exceeded")
    return RetryDecision(True, delay, "transient_failure")
