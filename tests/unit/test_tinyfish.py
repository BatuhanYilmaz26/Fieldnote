import json

import httpx
import pytest

from fieldnote.providers.base import ProviderError
from fieldnote.providers.tinyfish import TinyFishResearchGateway

pytestmark = pytest.mark.asyncio


async def test_direct_search_and_batch_bind_evidence_to_requested_urls():
    first = "https://example.org/first"
    second = "https://example.org/second"
    third = "https://example.org/unavailable"
    fourth = "https://example.org/unsafe-redirect"
    calls = []

    def transport(request):
        calls.append(request.method)
        assert request.headers["X-API-Key"] == "test-key"
        if request.method == "GET":
            assert request.url.host == "api.search.tinyfish.ai"
            assert dict(request.url.params) == {
                "query": "website availability", "domain_type": "web", "page": "0", "language": "de",
            }
            return httpx.Response(200, json={"results": [{"url": first}, {"url": first}, {"url": second}]})
        assert str(request.url) == "https://api.fetch.tinyfish.ai/"
        request_body = json.loads(request.content)
        assert request_body["urls"] == [first, second, third, fourth]
        assert request_body["ttl"] == 0 and request_body["include_etag_and_last_modified"]
        mirror = "https://example.com/shared"
        return httpx.Response(200, json={"results": [
            {"url": second, "final_url": mirror, "text": "Second body"},
            {"url": third, "error": "not available"},
            {"url": fourth, "final_url": "http://127.0.0.1", "text": "Unsafe body"},
            {"url": "https://example.org/unrequested", "text": "Unexpected"},
            {"url": first, "final_url": mirror, "text": "First body", "published_date": "2024-01-01",
             "last_modified": "Fri, 01 Nov 2024 00:00:00 GMT"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        gateway = TinyFishResearchGateway(client, "test-key", qualify_dns=False)
        search = await gateway.search("website availability", language="de")
        evidence = await gateway.extract_many((first, second, third, fourth))

    assert calls == ["GET", "POST"]
    assert search.cost_micro_usd == evidence.cost_micro_usd == 0
    assert [row["url"] for row in search] == [first, second]
    assert [row["source"]["original_url"] for row in evidence] == [first, second]
    assert [row["excerpt"]["text"] for row in evidence] == ["First body", "Second body"]
    assert evidence[0]["source"]["id"] != evidence[1]["source"]["id"]
    source = evidence[0]["source"]
    assert source["published_at"] == "2024-01-01T00:00:00+00:00"
    assert source["origin_last_modified_at"] == "2024-11-01T00:00:00+00:00"
    assert "updated_at" not in source


@pytest.mark.parametrize(("status", "code", "cost"), [
    (302, "research_unavailable", 0),
    (402, "research_budget_exhausted", 0),
    (429, "research_rate_limited", 0),
    (None, "research_connection_failed", None),
])
async def test_direct_search_never_retries_or_redirects(status, code, cost):
    calls = []

    def transport(request):
        calls.append(request.url.host)
        if status is None:
            raise httpx.ConnectTimeout("no response", request=request)
        return httpx.Response(status, headers={"Location": "https://example.org/", "Retry-After": "22"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        with pytest.raises(ProviderError, match=code) as caught:
            await TinyFishResearchGateway(client, "test-key").search("example")

    assert calls == ["api.search.tinyfish.ai"]
    assert caught.value.research_cost_micro == cost
    assert caught.value.retryable is (status == 429)
    assert caught.value.outcome_unknown is (status is None)
