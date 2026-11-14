import asyncio
import json
import traceback
from dataclasses import asdict
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from fieldnote.domain import ContentPlan
from fieldnote.providers import (
    FixtureGateway,
    FixtureResearchGateway,
    OpenRouterGateway,
    ProviderError,
)
from fieldnote.providers.base import ResearchResults
from fieldnote.providers.treg import TregResearchGateway


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    count: int = Field(ge=1, le=8)


def completion(**changes):
    payload = {
        "id": "gen-test",
        "model": "vendor/actual-free-model",
        "provider": "TestProvider",
        "choices": [
            {"finish_reason": "stop", "message": {"content": '{"text":"hello","count":2}'}}
        ],
    }
    payload.update(changes)
    return payload


@pytest.mark.asyncio
async def test_openrouter_uses_strict_schema_fixed_origin_and_no_hidden_fallback():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json=completion(usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": "0"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = OpenRouterGateway(client, "test-secret")
        result = await gateway.generate(Answer, [{"role": "user", "content": "Write"}], "run:plan")
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-secret"
    body = json.loads(request.content)
    assert body["provider"] == {
        "require_parameters": True,
        "data_collection": "deny",
        "allow_fallbacks": False,
        "max_price": {"prompt": 0, "completion": 0, "request": 0},
    }
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["stream"] is False and "models" not in body
    assert result.data == Answer(text="hello", count=2)
    assert result.resolved_model == "vendor/actual-free-model"
    assert result.cost_usd == Decimal("0") and result.usage["prompt_tokens"] == 10


@pytest.mark.asyncio
async def test_absent_usage_and_price_remain_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as client:
        result = await OpenRouterGateway(client, "test-key").generate(
            Answer, [{"role": "user", "content": "Write"}], "call"
        )
    assert result.usage is None and result.cost_usd is None


@pytest.mark.asyncio
async def test_openrouter_total_timeout_bounds_a_response_with_continuous_chunks():
    closing = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.005)

        async def aclose(self):
            closing.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=SlowBody()),
    )) as client:
        with pytest.raises(ProviderError, match="provider_timeout") as error:
            await asyncio.wait_for(
                OpenRouterGateway(client, "test-key", timeout=0.05).generate(
                    Answer, [{"role": "user", "content": "Write"}], "call",
                ),
                timeout=2,
            )
    assert closing.is_set()
    assert error.value.retryable and error.value.outcome_unknown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "provider_authentication_failed", False),
        (402, "provider_budget_exhausted", False),
        (400, "provider_request_rejected", False),
        (429, "provider_rate_limited", True),
        (503, "provider_unavailable", True),
        (302, "provider_unavailable", False),
    ],
)
async def test_error_classification_is_safe_and_does_not_retry(status, code, retryable):
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Retry-After": "31", "Location": "https://attacker.example"},
            text="private upstream error test-key",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, "test-key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )
    assert error.value.code == code and error.value.retryable is retryable
    assert error.value.retry_after == 31
    assert "test-key" not in str(error.value)
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (completion(choices=[]), "provider_schema_invalid"),
        (
            completion(choices=[{"finish_reason": "length", "message": {"content": "{}"}}]),
            "provider_incomplete_output",
        ),
        (
            completion(
                choices=[{"finish_reason": "stop", "message": {"content": "{}", "refusal": "no"}}]
            ),
            "provider_refusal",
        ),
        (
            completion(choices=[{"finish_reason": "stop", "message": {"content": ""}}]),
            "provider_schema_invalid",
        ),
        (
            completion(
                choices=[
                    {"finish_reason": "stop", "message": {"content": '{"text":"x","count":99}'}}
                ]
            ),
            "provider_schema_invalid",
        ),
        (completion(error={"message": "secret"}), "provider_invalid_response"),
    ],
)
async def test_invalid_or_truncated_output_is_never_accepted(payload, code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ProviderError, match=code):
            await OpenRouterGateway(client, "test-key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("content,kind", [
    ("private upstream malformed JSON", "json"),
    ('{"text":"private content","count":99,"private-key":"private-value"}', "schema"),
])
async def test_validation_diagnostics_preserve_only_category_attribution_and_billing(content, kind):
    api_key = "private-token"
    payload = completion(
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": "0",
               "private-account": "private-value"},
        choices=[{"finish_reason": "stop", "message": {"content": content}}],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload),
    )) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, api_key).generate(
                Answer, [{"role": "user", "content": "Write"}], "call",
            )
    failure = error.value
    assert failure.validation_kind == kind and failure.schema_name == "Answer"
    assert failure.response is not None
    assert failure.response.cost_usd == Decimal("0")
    assert failure.response.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert failure.response.resolved_model == "vendor/actual-free-model"
    assert failure.response.provider == "TestProvider" and failure.response.request_id == "gen-test"
    saved = json.dumps(asdict(failure.response), default=str) + "".join(traceback.format_exception(failure))
    assert "private" not in saved


@pytest.mark.asyncio
async def test_cross_field_plan_failure_is_distinct_from_json_or_field_validation():
    plan = {
        "title": "A story", "target_words": 500, "tasks": [
            {"id": "opening", "order": 1, "title": "Opening", "goal": "Begin", "target_words": 200},
            {"id": "ending", "order": 2, "title": "Ending", "goal": "End", "target_words": 200},
        ],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json=completion(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]),
    ))) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, "test-key").generate(
                ContentPlan, [{"role": "user", "content": "Write"}], "plan",
            )
    assert error.value.validation_kind == "semantics"
    assert error.value.schema_name == "ContentPlan"
    assert error.value.response.cost_usd is None


@pytest.mark.asyncio
async def test_envelope_failure_preserves_billing_but_rejects_invalid_metadata_identifiers():
    payload = completion(
        id="private\nrequest", model="private\nmodel", provider="private\nprovider",
        choices=[], usage={"total_tokens": 9, "cost": "0"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, "test-key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call",
            )
    failure = error.value
    assert failure.validation_kind == "envelope"
    assert failure.response.cost_usd == 0 and failure.response.usage == {"total_tokens": 9}
    assert failure.response.request_id is None
    assert failure.response.resolved_model is None and failure.response.provider is None


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [
    {"total_tokens": 10_000_001, "cost": "1e100"},
    {"prompt_tokens": 10_000_000, "completion_tokens": 1, "cost": True},
    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 99, "cost": "NaN"},
])
async def test_unusable_accounting_values_remain_unknown(usage):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json=completion(usage=usage),
    ))) as client:
        result = await OpenRouterGateway(client, "test-key").generate(
            Answer, [{"role": "user", "content": "Write"}], "call",
        )
    assert result.usage is None and result.cost_usd is None


@pytest.mark.asyncio
async def test_transport_timeout_has_uncertain_remote_outcome():
    def respond(_):
        raise httpx.ReadTimeout("private failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError) as error:
            await OpenRouterGateway(client, "key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )
    assert error.value.code == "provider_timeout" and error.value.outcome_unknown


@pytest.mark.asyncio
async def test_oversize_response_is_bounded():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1_048_577))
    ) as client:
        with pytest.raises(ProviderError, match="provider_response_too_large"):
            await OpenRouterGateway(client, "key").generate(
                Answer, [{"role": "user", "content": "Write"}], "call"
            )


def test_free_only_rejects_paid_model_and_untrusted_origin():
    client = httpx.AsyncClient()
    with pytest.raises(ValueError, match="Free-only"):
        OpenRouterGateway(client, "key", model="vendor/paid")
    with pytest.raises(ValueError, match="official"):
        OpenRouterGateway(client, "key", base_url="https://example.com/api/v1")
    with pytest.raises(ValueError, match="allowlists"):
        OpenRouterGateway(client, "key", model="vendor/paid", policy="budgeted")


@pytest.mark.asyncio
async def test_budgeted_transport_requires_trusted_worker_reservation():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as client:
        gateway = OpenRouterGateway(
            client,
            "key",
            model="vendor/paid",
            policy="budgeted",
            allowed_models=("vendor/paid",),
            allowed_providers=("vendor",),
        )
        with pytest.raises(ProviderError, match="budget_reservation_required"):
            await gateway.generate(Answer, [{"role": "user", "content": "Write"}], "call")


@pytest.mark.asyncio
async def test_fixture_payloads_are_validated_and_explicitly_labeled():
    gateway = FixtureGateway({"Answer": {"text": "Synthetic fixture", "count": 2}})
    result = await gateway.generate(Answer, [], "fixture:plan")
    assert result.fixture and result.provider == "fixture" and result.cost_usd == 0
    assert gateway.calls == ["fixture:plan"]
    with pytest.raises(ProviderError, match="fixture_response_not_registered"):
        await FixtureGateway().generate(Answer, [], "fixture:missing")


@pytest.mark.asyncio
async def test_fixture_research_is_deterministic_and_empty_corpus_stays_empty():
    assert await FixtureResearchGateway().search("topic") == []
    gateway = FixtureResearchGateway(
        {"https://example.com/article": {"title": "Testing", "text": "Synthetic local evidence."}}
    )
    result = await gateway.extract("https://example.com/article")
    assert result == await gateway.extract("https://example.com/article")
    assert result["fixture"] is True and "Synthetic fixture" in result["source"]["title"]
    assert len(result["source"]["content_hash"]) == 64


@pytest.mark.asyncio
async def test_treg_free_only_route_cap_and_no_hidden_paid_waterfall():
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="zero spend cap"):
            TregResearchGateway(client, "token", routing_policy="free_only", max_cost_micro=1)
        with pytest.raises(ValueError, match="positive spend cap"):
            TregResearchGateway(client, "token", routing_policy="budgeted", max_cost_micro=0)
        gateway = TregResearchGateway(client, "test-token", org="org-1")
        headers = gateway._headers()
        assert headers["X-Treg-Route-Max-Cost"] == "0"
        assert headers["X-Treg-Route-Waterfall"] == "0"
        assert headers["X-Treg-Org"] == "org-1"


@pytest.mark.asyncio
async def test_treg_search_and_extract_normalize_evidence_with_zero_charge():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tinyfish.web.search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://example.org/article",
                            "title": " Example article ",
                            "snippet": " Example snippet ",
                        }
                    ]
                },
                headers={
                    "x-treg-cost-micro": "0",
                    "x-treg-routed-tool": "treg.web.search",
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Fetched title",
                        "text": "Fetched evidence text.",
                        "final_url": "https://example.org/article",
                        "published_date": "2024-01-02",
                    }
                ],
                "errors": [],
            },
            headers={"x-treg-cost-micro": "0", "x-treg-routed-tool": "treg.web.extract"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = TregResearchGateway(client, "test-token", qualify_dns=False)
        found = await gateway.search("example query", max_results=1)
        assert found == [
            {
                "url": "https://example.org/article",
                "title": "Example article",
                "snippet": "Example snippet",
                "provider": "treg/tinyfish",
                "published_at": None,
            }
        ]
        assert isinstance(found, ResearchResults) and found.cost_micro_usd == 0
        extracted = await gateway.extract("https://example.org/article")
        assert extracted["source"]["content_hash"] == extracted["excerpt"]["content_hash"]
        assert extracted["source"]["published_at"].startswith("2024-01-02")
        assert extracted["cost_micro_usd"] == 0
        assert extracted["fixture"] is False
    assert requests[0].headers["X-Treg-Token"] == "test-token"
    assert requests[0].headers["X-Treg-Route-Max-Cost"] == "0"
    assert requests[0].headers["X-Treg-Route-Waterfall"] == "0"


@pytest.mark.asyncio
async def test_treg_rejects_paid_charge_unknown_billing_and_unsafe_urls():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"results": []}, headers={"x-treg-cost-micro": "5"}
            )
        )
    ) as client:
        gateway = TregResearchGateway(client, "token", qualify_dns=False)
        with pytest.raises(ProviderError, match="research_cost_policy_violation"):
            await gateway.search("query")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []}))
    ) as client:
        gateway = TregResearchGateway(client, "token", qualify_dns=False)
        with pytest.raises(ProviderError, match="research_billing_unknown"):
            await gateway.search("query")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": {"code": "INVALID_INPUT"}})
        )
    ) as client:
        gateway = TregResearchGateway(client, "token", qualify_dns=False)
        with pytest.raises(ProviderError, match="research_request_rejected"):
            await gateway.search("query")
        with pytest.raises(ValueError):
            await gateway.extract("http://127.0.0.1/private")
