import json

import httpx
import pytest

from fieldnote.providers.qualification import qualify_openrouter, read_openrouter_quota


def completion(cost="0", provider="TestProvider"):
    usage = {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": "vendor/free-model", "provider": provider, "usage": usage,
        "choices": [{"finish_reason": "stop", "message": {"content": '{"status":"ready"}'}}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("remaining", "exhausted"), [(0, True), (41, False), (None, None)])
async def test_quota_only_checks_key_without_dispatching_or_leaking_metadata(remaining, exhausted):
    calls = []

    def respond(request):
        calls.append((request.method, str(request.url)))
        data = {"label": "private account label"}
        if remaining is not None:
            data["free_model_daily_requests"] = {"remaining": remaining, "limit": 50, "private": "omit"}
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        quota = await read_openrouter_quota(client, "private-key")
    assert calls == [("GET", "https://openrouter.ai/api/v1/key")]
    assert quota == {
        "free_model_daily_requests": {"remaining": remaining, "limit": 50} if remaining is not None else None,
        "exhausted": exhausted,
    }


@pytest.mark.asyncio
async def test_qualification_uses_production_policy_and_excludes_account_secrets():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {
                "label": "private-account-label", "hash": "private-key-hash",
                "creator_user_id": "private-user", "is_free_tier": True,
                "free_model_daily_requests": {"limit": 50, "remaining": 49, "private": "omit"},
            }})
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await qualify_openrouter(client, "private-token")
    assert report["qualified"] is True
    assert len(calls) == 2
    assert calls[0].url == "https://openrouter.ai/api/v1/key"
    body = json.loads(calls[1].content)
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0}
    assert body["provider"]["data_collection"] == "deny"
    assert body["provider"]["allow_fallbacks"] is False
    serialized = json.dumps(report)
    assert all(secret not in serialized for secret in (
        "private-token", "private-account-label", "private-key-hash", "private-user", "omit",
    ))
    assert report["account"]["free_model_daily_requests"]["remaining"] == 49


@pytest.mark.asyncio
@pytest.mark.parametrize("cost,provider,code", [
    (None, "TestProvider", "provider_price_unknown"),
    ("0.01", "TestProvider", "provider_cost_policy_violation"),
    ("0", None, "qualification_response_metadata_missing"),
])
async def test_qualification_requires_known_zero_cost_and_attribution(cost, provider, code):
    def respond(request):
        return httpx.Response(200, json=(
            {"data": {"is_free_tier": True}} if request.url.path.endswith("/key")
            else completion(cost, provider)
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await qualify_openrouter(client, "test-token")
    assert report["qualified"] is False and report["code"] == code


@pytest.mark.asyncio
async def test_failed_account_authentication_never_dispatches_or_logs_upstream_body():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "private upstream detail private-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await qualify_openrouter(client, "private-token")
    assert report["code"] == "provider_authentication_failed"
    assert report["qualified"] is False and len(calls) == 1
    assert "private" not in json.dumps(report)


@pytest.mark.asyncio
async def test_exhausted_account_quota_never_dispatches():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"free_model_daily_requests": {
            "limit": 50, "remaining": 0,
        }}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await qualify_openrouter(client, "test-token")
    assert report["code"] == "provider_daily_quota_exhausted"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_rate_limited_probe_stops_after_one_inference_and_retains_wait():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"is_free_tier": True}})
        return httpx.Response(
            429, headers={"Retry-After": "120"},
            json={"error": "private upstream detail private-token"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await qualify_openrouter(client, "private-token")
    assert report["qualified"] is False
    assert report["code"] == "provider_rate_limited"
    assert report["retryable"] is True
    assert report["retry_after_seconds"] == 120
    assert len(calls) == 2
    assert [request.method for request in calls] == ["GET", "POST"]
    assert "private" not in json.dumps(report)


@pytest.mark.asyncio
async def test_paid_model_is_rejected_before_even_account_lookup():
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(500),
    )) as client:
        with pytest.raises(ValueError):
            await qualify_openrouter(client, "test-token", model="vendor/paid-model")
    assert calls == []


@pytest.mark.asyncio
async def test_rejected_probe_keeps_safe_billing_metadata_without_qualifying():
    def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"is_free_tier": True}})
        payload = completion()
        payload["choices"][0]["message"]["content"] = '{"status":"private invalid content"}'
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await qualify_openrouter(client, "private-key")
    assert report["qualified"] is False and report["code"] == "provider_schema_invalid"
    assert report["probe"]["schema_valid"] is False
    assert report["probe"]["cost_usd"] == "0"
    assert report["probe"]["resolved_model"] == "vendor/free-model"
    assert report["probe"]["validation_kind"] == "schema"
    assert report["probe"]["schema_name"] == "QualificationProbe"
    assert "private" not in json.dumps(report)
