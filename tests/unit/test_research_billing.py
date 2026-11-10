"""An uncertain bill or monetary-policy violation stops the whole research run."""

import httpx
import pytest

from fieldnote.providers.base import ProviderError
from fieldnote.providers.treg import TregResearchGateway
from fieldnote.services.research import ResearchCoordinator


@pytest.mark.parametrize("stage", ["search", "extract"])
@pytest.mark.parametrize("policy,header,code", [
    ("free_only", None, "research_billing_unknown"),
    ("free_only", "invalid", "research_billing_unknown"),
    ("free_only", "1", "research_cost_policy_violation"),
    ("budgeted", "11", "research_route_cap_exceeded"),
])
async def test_production_transport_stops_collection_on_charge_failure(stage, policy, header, code):
    calls = []

    def respond(request):
        calls.append(request)
        extracting = request.method == "POST"
        failing = stage == "search" or extracting
        headers = {"X-Treg-Cost-Micro": "0"}
        if failing:
            headers = {} if header is None else {"X-Treg-Cost-Micro": header}
        return httpx.Response(200, headers=headers, json={"results": [
            {"url": "https://example.org/a", "title": "A"},
            {"url": "https://example.org/b", "title": "B"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = TregResearchGateway(client, "synthetic-token", qualify_dns=False,
                                      routing_policy=policy, max_cost_micro=10 if policy == "budgeted" else 0)
        with pytest.raises(ProviderError, match=code) as error:
            await ResearchCoordinator(gateway).collect(["one", "two"])
    assert error.value.outcome_unknown
    assert not error.value.retryable
    assert len(calls) == (1 if stage == "search" else 3)
    assert sum(request.method == "POST" for request in calls) == (0 if stage == "search" else 1)
