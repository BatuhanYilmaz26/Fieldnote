"""Source-bound evidence persists through the actual worker without rewriting legacy IDs."""

from pathlib import Path

import pytest

from fieldnote.domain import CreateRunRequest
from fieldnote.graph import make_fixture_dependencies
from fieldnote.providers.fixture import FixtureResearchGateway
from fieldnote.settings import Settings
from fieldnote.storage import Repository
from fieldnote.storage.pool import database_pool
from fieldnote.worker.runner import ProviderRuntime, process_claim

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest.mark.asyncio
async def test_worker_preserves_mirror_sources_and_preexisting_legacy_excerpt(acceptance_database):
    settings = Settings(_env_file=None, database_url=acceptance_database)
    request = CreateRunRequest(topic="Technical source", target_words=350)
    base = make_fixture_dependencies(request)
    pages = dict(base.research._pages)
    for suffix in ("mirror-a", "mirror-b"):
        pages[f"https://example.org/{suffix}"] = {"title": "Technical mirror", "text": "The same source body appears at two original URLs."}
    research = FixtureResearchGateway(pages)
    original_url = next(iter(base.research._pages))
    old = await research.extract(original_url)
    legacy = {**old["excerpt"], "id": "excerpt_legacy_retained"}
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, acceptance_database)
        run = await repository.create_run(settings.local_owner_id, request.model_dump(mode="json"), "source-mirrors")
        claim = await repository.claim_job("provenance-worker")
        assert claim is not None
        await repository.save_sources(claim, [old["source"]], [legacy])
        await process_claim(repository, claim, settings, ProviderRuntime(research=research))
        saved = await repository.get_run(settings.local_owner_id, run["id"])
        assert saved["state"] == "needs_review"
        sources = await repository.sources(settings.local_owner_id, run["id"])
        assert len(sources) == 3
        async with pool.connection() as connection:
            rows = await (await connection.execute("SELECT id,source_id,excerpt_data FROM evidence_excerpts WHERE run_id=%s", (run["id"],))).fetchall()
        assert len(rows) == 4
        assert next(row["excerpt_data"] for row in rows if row["id"] == legacy["id"]) == legacy
        assert len({row["source_id"] for row in rows}) == 3
        article = await repository.get_article(settings.local_owner_id, run["id"])
        assert article["content_hash"] and article["review_status"] == "needs_review"
