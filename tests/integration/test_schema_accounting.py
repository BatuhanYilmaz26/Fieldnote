"""Rejected AI content retains safe diagnostics and separate billing evidence."""

import json
from decimal import Decimal

import httpx
import pytest

from fieldnote.providers.openrouter import OpenRouterGateway
from fieldnote.settings import Settings
from fieldnote.worker.runner import ProviderRuntime, process_claim


@pytest.mark.asyncio
@pytest.mark.parametrize("repairs,cost,expected_calls,expected_code", [
    (0, "0", 1, "provider_schema_invalid"),
    (1, "0", 2, "provider_schema_invalid"),
    (1, None, 2, "provider_schema_invalid"),
    (1, "0.001", 1, "provider_cost_policy_violation"),
])
async def test_rejected_plan_has_bounded_repairs_accounting_and_safe_api_error(
    api_client, acceptance_database, repairs, cost, expected_calls, expected_code,
):
    client, app = api_client
    settings = Settings(
        _env_file=None, database_url=acceptance_database, model_provider="openrouter",
        openrouter_api_key="test-key", openrouter_qualified=True, enable_external_calls=True,
        openrouter_schema_repairs=repairs, max_job_attempts=1,
        provider_rpm=10, provider_tpm=100_000,
    )
    created = await client.post("/api/v1/runs", json={
        "topic": "Write a short story about a dragon", "research_mode": "off", "target_words": 500,
    }, headers={"Idempotency-Key": "schema-accounting"})
    assert created.status_code == 202
    run_id = created.json()["id"]
    invalid_plan = {
        "title": "private generated article title", "target_words": 500, "tasks": [
            {"id": "opening", "order": 1, "title": "Opening", "goal": "Begin", "target_words": 200},
            {"id": "ending", "order": 2, "title": "Ending", "goal": "End", "target_words": 200},
        ],
    }
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        if cost is not None:
            usage["cost"] = cost
        return httpx.Response(200, json={
            "id": "gen-rejected", "model": "vendor/returned-free-model", "provider": "TestProvider",
            "usage": usage,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(invalid_plan)}}],
        })

    claim = await app.state.repository.claim_job("schema-accounting-worker")
    assert claim is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        runtime = ProviderRuntime(model=OpenRouterGateway(transport, "test-key"))
        await process_claim(app.state.repository, claim, settings, runtime)

    assert len(sent) == expected_calls
    status = await client.get(f"/api/v1/runs/{run_id}")
    assert status.status_code == 200
    run = status.json()
    assert run["state"] == "failed" and run["error_code"] == expected_code
    assert run["model_call_count"] == expected_calls
    if expected_code == "provider_schema_invalid":
        assert "required writing format" in run["error_message"]
    assert "private" not in json.dumps(run)
    assert (await client.get(f"/api/v1/runs/{run_id}/article")).status_code == 404

    async with app.state.repository.pool.connection() as connection:
        calls = await (await connection.execute(
            "SELECT p.call_id,p.status,p.actual_tokens,p.metadata,b.actual_usd,b.status AS billing_status "
            "FROM provider_calls p JOIN budget_reservations b ON b.run_id=p.run_id AND b.call_id=p.call_id "
            "WHERE p.run_id=%s ORDER BY p.created_at", (run_id,),
        )).fetchall()
        plans = await (await connection.execute(
            "SELECT count(*) AS count FROM plans WHERE run_id=%s", (run_id,),
        )).fetchone()
    assert plans["count"] == 0 and len(calls) == expected_calls
    for call in calls:
        assert call["status"] == "failed" and call["actual_tokens"] == 30
        assert call["actual_usd"] == (Decimal(cost) if cost is not None else None)
        assert call["billing_status"] == ("reconciled" if cost is not None else "unknown")
        assert call["metadata"]["validation_kind"] == "semantics"
        assert call["metadata"]["schema_name"] == "ContentPlan"
        assert call["metadata"]["resolved_model"] == "vendor/returned-free-model"
        assert call["metadata"]["provider"] == "TestProvider"
        assert call["metadata"]["request_id"] == "gen-rejected"
        assert "private" not in json.dumps(call["metadata"])
    if expected_calls == 2:
        assert calls[1]["call_id"].endswith(":repair1")
        assert "relationships between fields" in sent[1]["messages"][-1]["content"]
    assert all("private" not in json.dumps(body) for body in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(("finish", "cost", "expected"), [
    ("length", "0", 2), ("length", None, 1), ("content_filter", "0", 1),
])
async def test_only_known_free_length_cutoff_can_use_repair(api_client, acceptance_database, finish, cost, expected):
    client, app = api_client
    settings = Settings(
        _env_file=None, database_url=acceptance_database, model_provider="openrouter",
        openrouter_api_key="test-key", openrouter_qualified=True, enable_external_calls=True,
        provider_rpm=10, provider_tpm=100_000, max_job_attempts=1,
    )
    created = await client.post("/api/v1/runs", json={
        "topic": "Write a short story about a dragon", "research_mode": "off", "target_words": 500,
    }, headers={"Idempotency-Key": "length-accounting"})
    run_id = created.json()["id"]
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        if cost is not None:
            usage["cost"] = cost
        return httpx.Response(200, json={
            "model": "vendor/returned-free-model", "provider": "TestProvider", "usage": usage,
            "choices": [{"finish_reason": finish, "message": {"content": "private partial output"}}],
        })

    claim = await app.state.repository.claim_job("length-accounting-worker")
    assert claim is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        await process_claim(app.state.repository, claim, settings,
                            ProviderRuntime(model=OpenRouterGateway(transport, "test-key")))
    saved = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert saved["model_call_count"] == len(sent) == expected
    assert saved["error_code"] == "provider_incomplete_output"
    assert (await client.get(f"/api/v1/runs/{run_id}/article")).status_code == 404
    if expected == 2:
        assert "reached the output limit" in sent[1]["messages"][-1]["content"]
        assert sent[1]["max_tokens"] == sent[0]["max_tokens"]
    assert "private partial output" not in json.dumps(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,code,description", [
    (404, "provider_capability_unavailable", "currently unavailable"),
    (429, "provider_rate_limited", "request limit was reached"),
])
async def test_provider_availability_error_is_actionable_and_never_triggers_schema_repair(
    api_client, acceptance_database, http_status, code, description,
):
    client, app = api_client
    settings = Settings(
        _env_file=None, database_url=acceptance_database, model_provider="openrouter",
        openrouter_api_key="test-key", openrouter_qualified=True, enable_external_calls=True,
        openrouter_schema_repairs=1, max_job_attempts=1,
    )
    created = await client.post("/api/v1/runs", json={
        "topic": "Write a short story about a dragon", "research_mode": "off", "target_words": 500,
    }, headers={"Idempotency-Key": "provider-unavailable"})
    assert created.status_code == 202
    run_id = created.json()["id"]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(http_status, headers={"Retry-After": "120"},
                              text="private upstream response and credentials")

    claim = await app.state.repository.claim_job("unavailable-worker")
    assert claim is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        await process_claim(app.state.repository, claim, settings,
                            ProviderRuntime(model=OpenRouterGateway(transport, "test-key")))
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert len(calls) == 1 and run["model_call_count"] == 1
    assert run["state"] == "failed" and run["error_code"] == code
    assert description in run["error_message"] and "private" not in json.dumps(run)
    assert (await client.get(f"/api/v1/runs/{run_id}/article")).status_code == 404
