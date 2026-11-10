"""Research dispatch barriers through real worker and PostgreSQL state."""

from pathlib import Path

import pytest

from fieldnote.domain import CreateRunRequest
from fieldnote.providers.fixture import FixtureResearchGateway
from fieldnote.settings import Settings
from fieldnote.storage import Repository
from fieldnote.storage.pool import database_pool
from fieldnote.worker.runner import ProviderRuntime, process_claim

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["cancel", "deadline", "lease"])
async def test_worker_stops_research_after_durable_barrier(acceptance_database, barrier):
    settings = Settings(_env_file=None, database_url=acceptance_database)
    request = CreateRunRequest(topic="Technical sources", target_words=350)
    extracts = []

    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database)
        run = await repository.create_run(settings.local_owner_id, request.model_dump(mode="json"), f"research-{barrier}")
        claim = await repository.claim_job("research-admission")
        assert claim is not None

        class InterruptDuringSearch(FixtureResearchGateway):
            async def search(self, query, max_results=5):
                rows = await super().search(query, max_results)
                if barrier == "cancel":
                    await repository.cancel_run(settings.local_owner_id, run["id"])
                else:
                    async with pool.connection() as connection:
                        if barrier == "deadline":
                            await connection.execute("UPDATE runs SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s", (run["id"],))
                        else:
                            await connection.execute("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (run["id"],))
                return rows

            async def extract(self, url):
                extracts.append(url)
                return await super().extract(url)

        research = InterruptDuringSearch({"https://example.org/a": {"title": "Technical source", "text": "Technical source text."}})
        await process_claim(repository, claim, settings, ProviderRuntime(research=research))
        assert extracts == []
        events = await repository.list_events(settings.local_owner_id, run["id"])
        assert not any(event["event_type"] in {"plan_ready", "source_added", "section_completed", "completed"} for event in events)
