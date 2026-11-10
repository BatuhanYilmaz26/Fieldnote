"""Mirrored text retains separate provenance; historical IDs stay readable."""

import hashlib

import httpx
import pytest

from fieldnote.domain.models import EvidenceExcerpt, excerpt_identity
from fieldnote.providers.base import ProviderError
from fieldnote.providers.fixture import FixtureResearchGateway
from fieldnote.providers.tavily import TavilyResearchGateway
from fieldnote.providers.treg import TregResearchGateway
from fieldnote.services.research import ResearchCoordinator


def test_source_and_locator_both_contribute_to_new_excerpt_identity():
    digest = hashlib.sha256(b"same text").hexdigest()
    first = excerpt_identity("src_a", digest, "characters 0:9")
    assert first == excerpt_identity("src_a", digest, "characters 0:9")
    assert first != excerpt_identity("src_b", digest, "characters 0:9")
    assert first != excerpt_identity("src_a", digest, "characters 9:18")
    legacy = "excerpt_" + digest[:16]
    value = EvidenceExcerpt(id=legacy, source_id="src_a", text="same text", content_hash=digest,
                            locator="characters 0:9", source_kind="body")
    assert value.id == legacy


@pytest.mark.parametrize("provider", ["fixture", "treg", "tavily"])
async def test_mirrored_bodies_preserve_every_source(provider):
    urls = ("https://example.org/a", "https://example.org/b")
    text = "Identical mirrored technical source text."

    def respond(request):
        row = {"text": text, "raw_content": text, "title": "Technical source"}
        return httpx.Response(200, headers={"X-Treg-Cost-Micro": "0"}, json={"results": [row]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if provider == "fixture":
            gateway = FixtureResearchGateway({url: {"title": "Technical source", "text": text} for url in urls})
        elif provider == "treg":
            gateway = TregResearchGateway(client, "synthetic-token", qualify_dns=False)
        else:
            gateway = TavilyResearchGateway(client, "synthetic-token", qualify_dns=False)
        pages = [await gateway.extract(url) for url in urls]

    assert pages[0]["excerpt"]["id"] != pages[1]["excerpt"]["id"]
    assert pages[0]["excerpt"]["content_hash"] == pages[1]["excerpt"]["content_hash"]
    assert pages[0]["excerpt"]["text"] == pages[1]["excerpt"]["text"] == text
    for page in pages:
        assert page["excerpt"]["source_id"] == page["source"]["id"]

    class SavedPages:
        async def search(self, query, max_results=5):
            return [{"url": url, "title": "Technical source"} for url in urls]

        async def extract(self, url):
            return pages[urls.index(url)]

    outcome = await ResearchCoordinator(SavedPages()).collect(["technical"])
    assert len(outcome.sources) == len(outcome.excerpts) == 2
    assert set(outcome.sources) == {item["source_id"] for item in outcome.excerpts.values()}


async def test_coordinator_rejects_conflicting_provider_excerpt_identity():
    urls = ("https://example.org/a", "https://example.org/b")
    inner = FixtureResearchGateway({url: {"title": "Technical source", "text": "same text"} for url in urls})

    class Conflicting(FixtureResearchGateway):
        async def extract(self, url):
            value = await inner.extract(url)
            value["excerpt"]["id"] = "excerpt_legacy_collision"
            return value

    with pytest.raises(ProviderError, match="research_evidence_identity_conflict"):
        await ResearchCoordinator(Conflicting(inner._pages)).collect(["technical"])
