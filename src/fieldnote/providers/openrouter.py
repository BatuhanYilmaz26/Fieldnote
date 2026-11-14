"""Bounded, nonstreaming OpenRouter transport with application validation.

Retries, quotas, and durable reservation ownership belong to the worker's
dispatch service. This adapter never retries or chooses a different model.
"""

import asyncio
import json
import math
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from fieldnote.providers.base import ModelResult, ModelT, ProviderError, ResponseMetadata

_ORIGIN = "https://openrouter.ai/api/v1"
_MAX_RESPONSE_BYTES = 1_048_576
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/() -]{0,159}$")


def _strict_schema(value: Any) -> Any:
    """Produce a strict-output transport schema without mutating the model.

    Every object disallows unknown keys; fields with application defaults are
    requested explicitly. The original Pydantic model still validates values
    and cross-field invariants after the provider returns JSON.
    """
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _strict_schema(item) for key, item in value.items() if key != "default"}
    if result.get("type") == "object" and isinstance(result.get("properties"), dict):
        result["additionalProperties"] = False
        result["required"] = list(result["properties"])
    return result


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = (date - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, min(seconds, 86_400.0)) if math.isfinite(seconds) else None


def _usage(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, Decimal | None]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None, None
    normalized: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000:
            normalized[key] = value
    if "prompt_tokens" in normalized and "completion_tokens" in normalized:
        total = normalized["prompt_tokens"] + normalized["completion_tokens"]
        if total > 10_000_000 or ("total_tokens" in normalized and normalized["total_tokens"] != total):
            normalized = {}
    cost: Decimal | None = None
    raw_cost = raw.get("cost")
    if raw_cost is not None and not isinstance(raw_cost, bool):
        try:
            candidate = Decimal(str(raw_cost))
            if candidate.is_finite() and 0 <= candidate < 1_000_000_000:
                cost = candidate
        except (ValueError, InvalidOperation):
            pass
    return normalized or None, cost


def _identity(value: Any, *, label: bool = False) -> str | None:
    pattern = _SAFE_LABEL if label else _SAFE_ID
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


class OpenRouterGateway:
    """Use an injected reusable HTTPX client against the fixed official origin.

    Budgeted operation additionally requires explicit model/provider allowlists
    and a positive caller-supplied durable reservation amount. The reservation
    is not created by this class: passing an amount is a trusted worker boundary,
    never an HTTP request field or a substitute for a reservation ledger.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str = "openrouter/free",
        policy: Literal["free_only", "budgeted"] = "free_only",
        *,
        base_url: str = _ORIGIN,
        allowed_models: tuple[str, ...] = (),
        allowed_providers: tuple[str, ...] = (),
        zero_data_retention: bool = False,
        timeout: float = 90.0,
    ) -> None:
        if base_url.rstrip("/") != _ORIGIN:
            raise ValueError("Only the configured official OpenRouter origin is permitted")
        if not api_key or any(ch.isspace() for ch in api_key):
            raise ValueError("A valid OpenRouter API key is required")
        if not _SAFE_ID.fullmatch(model):
            raise ValueError("Invalid configured model identity")
        if policy not in {"free_only", "budgeted"}:
            raise ValueError("Unknown model policy")
        if policy == "free_only" and model != "openrouter/free" and not model.endswith(":free"):
            raise ValueError("Free-only policy rejects paid and unqualified model aliases")
        if policy == "budgeted" and (model not in allowed_models or not allowed_providers):
            raise ValueError("Budgeted calls require explicit model and provider allowlists")
        if any(not _SAFE_ID.fullmatch(provider) for provider in allowed_providers):
            raise ValueError("Invalid provider identity")
        if not 0 < timeout <= 180:
            raise ValueError("OpenRouter timeout must be between zero and 180 seconds")
        self._timeout = timeout
        self._client = client
        self._api_key = api_key
        self.model = model
        self.policy = policy
        self._providers = allowed_providers
        self._zdr = zero_data_retention

    async def generate(
        self,
        schema: type[ModelT],
        messages: list[dict[str, str]],
        call_identity: str,
        max_tokens: int = 2048,
        *,
        reserved_cost_usd: Decimal | None = None,
    ) -> ModelResult[ModelT]:
        if not _SAFE_ID.fullmatch(call_identity):
            raise ValueError("Invalid call identity")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 16 <= max_tokens <= 8192
        ):
            raise ValueError("Output token limit must be between 16 and 8192")
        if not messages or len(messages) > 32:
            raise ValueError("A bounded nonempty message list is required")
        if any(
            set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message["content"], str)
            for message in messages
        ):
            raise ValueError("Invalid model message")
        if sum(len(message["content"]) for message in messages) > 200_000:
            raise ValueError("Model input exceeds the transport character bound")
        if self.policy == "budgeted" and (
            reserved_cost_usd is None or not reserved_cost_usd.is_finite() or reserved_cost_usd <= 0
        ):
            raise ProviderError("budget_reservation_required")
        provider: dict[str, Any] = {
            "require_parameters": True,
            "data_collection": "deny",
            "allow_fallbacks": False,
        }
        if self.policy == "free_only":
            provider["max_price"] = {"prompt": 0, "completion": 0, "request": 0}
        if self._providers:
            provider["only"] = list(self._providers)
        if self._zdr:
            provider["zdr"] = True
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "temperature": 0.2,
            "provider": provider,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _strict_schema(schema.model_json_schema()),
                },
            },
        }
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    "POST",
                    f"{_ORIGIN}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}", "X-Title": "Fieldnote"},
                    json=body,
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        code = {
                            400: "provider_request_rejected",
                            401: "provider_authentication_failed",
                            402: "provider_budget_exhausted",
                            403: "provider_policy_denied",
                            404: "provider_capability_unavailable",
                            408: "provider_timeout",
                            429: "provider_rate_limited",
                        }.get(response.status_code, "provider_unavailable")
                        raise ProviderError(
                            code,
                            retryable=response.status_code in {408, 429} or response.status_code >= 500,
                            retry_after=_retry_after(response.headers.get("Retry-After")),
                            outcome_unknown=response.status_code == 408 or response.status_code >= 500,
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_RESPONSE_BYTES:
                            raise ProviderError("provider_response_too_large", outcome_unknown=True)
                    try:
                        payload = json.loads(chunks)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ProviderError("provider_invalid_response", outcome_unknown=True) from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderError("provider_timeout", retryable=True, outcome_unknown=True) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "provider_connection_failed", retryable=True, outcome_unknown=True
            ) from exc
        if not isinstance(payload, dict) or payload.get("error"):
            raise ProviderError("provider_invalid_response", outcome_unknown=True)
        usage, cost = _usage(payload)
        metadata = ResponseMetadata(
            requested_model=self.model, resolved_model=_identity(payload.get("model")),
            provider=_identity(payload.get("provider"), label=True),
            request_id=_identity(payload.get("id")), usage=usage, cost_usd=cost,
        )
        schema_name = _identity(schema.__name__)
        try:
            choices = payload["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected one response choice")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ProviderError(
                    "provider_incomplete_output", outcome_unknown=True, response=metadata, schema_name=schema_name,
                )
            message = choice["message"]
            if message.get("refusal"):
                raise ProviderError("provider_refusal", response=metadata)
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty content")
            data = schema.model_validate_json(content)
            resolved_model = metadata.resolved_model
            if resolved_model is None:
                raise ValueError("Missing resolved model identity")
        except ProviderError:
            raise
        except ValidationError as exc:
            # Error input, locations, context and messages can contain private
            # content. Retain only a fixed category, never the raw errors.
            types = {error["type"] for error in exc.errors(
                include_url=False, include_context=False, include_input=False,
            )}
            kind: Literal["json", "schema", "semantics", "envelope"] = (
                "json" if "json_invalid" in types else
                "semantics" if "value_error" in types else "schema"
            )
            raise ProviderError(
                "provider_schema_invalid", outcome_unknown=True, response=metadata,
                validation_kind=kind, schema_name=schema_name,
            ) from None
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ProviderError(
                "provider_schema_invalid", outcome_unknown=True, response=metadata,
                validation_kind="envelope", schema_name=schema_name,
            ) from None
        return ModelResult(
            data=data,
            requested_model=self.model,
            resolved_model=resolved_model,
            request_id=metadata.request_id,
            provider=metadata.provider,
            usage=usage,
            cost_usd=cost,
            duration_seconds=time.monotonic() - started,
        )
