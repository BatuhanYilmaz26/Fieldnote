"""A bounded account check using synthetic input and the production transport.

Qualification never enables a runtime setting. Reports contain selected quota
fields and response metadata, never credentials or upstream error bodies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

from fieldnote.providers.base import ProviderError
from fieldnote.providers.openrouter import OpenRouterGateway
from fieldnote.runtime import run_async

ORIGIN = "https://openrouter.ai/api/v1"
MAX_METADATA_BYTES = 2_097_152


class QualificationProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready"]


async def _metadata(client: httpx.AsyncClient, path: str, api_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if path == "/key" else {}
    try:
        async with client.stream(
            "GET", ORIGIN + path, headers=headers, follow_redirects=False,
            timeout=httpx.Timeout(20, connect=5),
        ) as response:
            if response.status_code != 200:
                code = {
                    401: "provider_authentication_failed", 403: "provider_policy_denied",
                    429: "provider_rate_limited",
                }.get(response.status_code, "qualification_metadata_unavailable")
                raise ProviderError(code)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    raise ProviderError("qualification_metadata_too_large")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ProviderError("qualification_metadata_invalid")
            return payload
    except (httpx.RequestError, ValueError, UnicodeError) as exc:
        raise ProviderError("qualification_metadata_unavailable") from exc


def _quota(data: dict[str, Any]) -> dict[str, Any]:
    """Exclude key labels, hashes, account IDs and unrecognized nested fields."""
    result: dict[str, Any] = {}
    if isinstance(data.get("is_free_tier"), bool):
        result["is_free_tier"] = data["is_free_tier"]
    for field in ("limit", "limit_remaining", "usage", "usage_daily"):
        value = data.get(field)
        if value is None or (type(value) in {int, float} and math.isfinite(value) and value >= 0):
            result[field] = value
    for field, allowed in (
        ("free_model_daily_requests", ("limit", "remaining", "used")),
        ("rate_limit", ("requests",)),
    ):
        value = data.get(field)
        if isinstance(value, dict):
            result[field] = {
                key: value[key] for key in allowed
                if type(value.get(key)) in {int, float}
                and math.isfinite(value[key]) and value[key] >= 0
            }
    return result


async def read_openrouter_quota(client: httpx.AsyncClient, api_key: str) -> dict[str, Any]:
    data = (await _metadata(client, "/key", api_key)).get("data")
    if not isinstance(data, dict):
        raise ProviderError("qualification_account_invalid")
    counter = _quota(data).get("free_model_daily_requests", {})
    remaining = counter.get("remaining")
    return {"free_model_daily_requests": counter or None,
            "exhausted": remaining == 0 if remaining is not None else None}


async def qualify_openrouter(
    client: httpx.AsyncClient, api_key: str, *, model: str = "openrouter/free",
) -> dict[str, Any]:
    """Perform one synthetic strict-schema call; never retry or weaken policy."""
    # Validate the model before even the account metadata request.
    gateway = OpenRouterGateway(client, api_key, model=model)
    report: dict[str, Any] = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "qualified": False,
        "requested_model": model,
        "policy": {
            "free_only": True, "data_collection": "deny",
            "require_parameters": True, "allow_fallbacks": False,
            "max_price": {"prompt": 0, "completion": 0, "request": 0},
        },
        "scope": "One synthetic strict-schema probe; not article quality or release acceptance.",
    }
    try:
        account = (await _metadata(client, "/key", api_key)).get("data")
        if not isinstance(account, dict):
            raise ProviderError("qualification_account_invalid")
        report["account"] = _quota(account)
        counter = report["account"].get("free_model_daily_requests", {})
        if counter.get("remaining") == 0:
            raise ProviderError("provider_daily_quota_exhausted")
        result = await gateway.generate(
            QualificationProbe,
            [{"role": "user", "content": 'Return exactly {"status":"ready"}.'}],
            "qualification:strict-schema", max_tokens=128,
        )
        report["probe"] = {
            "schema_valid": True, "requested_model": result.requested_model,
            "resolved_model": result.resolved_model, "provider": result.provider,
            "usage": result.usage,
            "cost_usd": str(result.cost_usd) if result.cost_usd is not None else None,
            "duration_seconds": round(result.duration_seconds, 3),
        }
        if result.cost_usd is None:
            raise ProviderError("provider_price_unknown")
        if result.cost_usd != 0:
            raise ProviderError("provider_cost_policy_violation")
        if not result.provider or not result.usage:
            raise ProviderError("qualification_response_metadata_missing")
        report["qualified"] = True
    except ProviderError as exc:
        if exc.response:
            report["probe"] = {
                "schema_valid": False,
                "requested_model": exc.response.requested_model,
                "resolved_model": exc.response.resolved_model,
                "provider": exc.response.provider,
                "usage": exc.response.usage,
                "cost_usd": str(exc.response.cost_usd) if exc.response.cost_usd is not None else None,
                "validation_kind": exc.validation_kind,
                "schema_name": exc.schema_name,
            }
        report["code"] = exc.code
        report["retryable"] = exc.retryable
        report["outcome_unknown"] = exc.outcome_unknown
        report["retry_after_seconds"] = exc.retry_after
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check OpenRouter with one synthetic, zero-price strict-schema request.",
    )
    parser.add_argument("--env-file", type=Path, help="Read only OPENROUTER_API_KEY from this file")
    parser.add_argument("--model", default="openrouter/free", help="Free router or an explicit :free model")
    parser.add_argument("--output", type=Path, default=Path(".local/openrouter/qualification.json"))
    parser.add_argument("--quota-only", action="store_true", help="Read today's free request quota without inference")
    args = parser.parse_args(argv)
    if not args.quota_only and args.output.exists():
        parser.error("Output already exists. Choose a new report filename.")
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("The requested environment file does not exist.")
        api_key = str(dotenv_values(args.env_file).get("OPENROUTER_API_KEY") or "")
    if not api_key.strip():
        parser.error("Set OPENROUTER_API_KEY or select an environment file containing it.")

    if args.quota_only:
        async def read_quota() -> dict[str, Any]:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                return await read_openrouter_quota(client, api_key)

        try:
            quota = run_async(read_quota())
        except ProviderError as exc:
            print(json.dumps({"code": exc.code}))
            return 1
        print(json.dumps(quota))
        return 0

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            return await qualify_openrouter(client, api_key, model=args.model)

    try:
        report = run_async(run())
    except ValueError:
        parser.error("Choose openrouter/free or an explicit :free model ID.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"qualified": report["qualified"], "code": report.get("code"),
                      "report": str(args.output)}))
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
