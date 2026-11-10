"""No further research is dispatched after a worker admission barrier fails."""

import pytest

from fieldnote.domain import CreateRunRequest
from fieldnote.graph import GraphHooks, build_graph, initial_state, make_fixture_dependencies
from fieldnote.providers.base import ProviderError
from fieldnote.providers.fixture import FixtureResearchGateway
from fieldnote.services.research import ResearchCoordinator


@pytest.mark.parametrize("code", ["cancelled", "deadline_exceeded", "lease_lost"])
async def test_admission_failure_stops_before_any_research_dispatch(code):
    class NeverDispatch:
        async def search(self, *args, **kwargs):
            pytest.fail("Search dispatched after admission failed")

        async def extract(self, *args, **kwargs):
            pytest.fail("Extraction dispatched after admission failed")

    async def check():
        raise ProviderError(code)

    with pytest.raises(ProviderError, match=code):
        await ResearchCoordinator(NeverDispatch(), check_active=check).collect(["topic"])


@pytest.mark.parametrize("stop_after", ["search", "extract"])
async def test_real_graph_rechecks_activity_between_research_requests(stop_after):
    stopped = False
    searches = []
    extracts = []
    pages = {
        f"https://example.org/{letter}": {"title": "Technical source", "text": f"Source {letter}"}
        for letter in ("a", "b")
    }

    class InterruptResearch(FixtureResearchGateway):
        async def search(self, query, max_results=5):
            nonlocal stopped
            searches.append(query)
            rows = await super().search(query, max_results)
            if stop_after == "search":
                stopped = True
            return rows

        async def extract(self, url):
            nonlocal stopped
            extracts.append(url)
            result = await super().extract(url)
            stopped = True
            return result

    async def check():
        if stopped:
            raise ProviderError("run_cancelled")

    request = CreateRunRequest(topic="Technical source", target_words=350)
    deps = make_fixture_dependencies(request, hooks=GraphHooks(check_active=check))
    deps.research = InterruptResearch(pages)
    with pytest.raises(ProviderError, match="run_cancelled"):
        await build_graph(deps).compile().ainvoke(initial_state())
    assert len(searches) == 1
    assert len(extracts) == (0 if stop_after == "search" else 1)
    assert deps.model.calls == []


async def test_coordinator_rechecks_activity_before_a_second_query():
    calls = []

    class SearchOnly:
        async def search(self, query, max_results=5):
            calls.append(query)
            return []

    async def check():
        if calls:
            raise ProviderError("lease_lost")

    with pytest.raises(ProviderError, match="lease_lost"):
        await ResearchCoordinator(SearchOnly(), check_active=check).collect(["first", "second"])
    assert calls == ["first"]
