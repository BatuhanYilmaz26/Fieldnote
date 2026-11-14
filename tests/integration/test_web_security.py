"""Browser-shell security and revision-preview acceptance."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from fieldnote.settings import Settings
from fieldnote.storage import Repository, StorageError
from fieldnote.storage.pool import database_pool
from fieldnote.worker.runner import process_claim

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
pytestmark = pytest.mark.asyncio


def brief() -> dict[str, object]:
    return {
        "topic": "Write a short story about a dragon",
        "audience": "General readers",
        "tone": "clear",
        "language": "en",
        "target_words": 500,
        "research_mode": "required",
        "instructions": "",
    }


async def run_one_fixture_job(url: str) -> None:
    settings = Settings(_env_file=None)
    async with database_pool(url, migrations_directory=MIGRATIONS) as pool:
        repository = Repository(pool, url, max_run_seconds=settings.max_run_seconds)
        await repository.worker_heartbeat("web-acceptance-worker")
        claim = await repository.claim_job("web-acceptance-worker")
        assert claim is not None
        await process_claim(repository, claim, settings)


async def test_browser_shell_enforces_csp_and_has_no_html_injection_sinks(
    api_client,
) -> None:
    client, _ = api_client
    response = await client.get("/")
    assert response.status_code == 200
    assert "Fieldnote — Writing studio" in response.text
    assert 'aria-label="Fieldnote home"' in response.text
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"

    html = response.text
    assert html.count("<script") == 1
    assert '<script type="module" src="/assets/app.js?desk=7"></script>' in html
    lowered = html.lower()
    assert "onclick=" not in lowered
    assert "onerror=" not in lowered
    assert "javascript:" not in lowered
    assert "<style" not in lowered

    script = await client.get("/assets/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    source = script.text
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "srcdoc",
    ):
        assert forbidden not in source, forbidden
    assert "createTextNode" in source
    assert "replaceChildren" in source

    styles = await client.get("/assets/styles.css")
    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]
    assert "@media (prefers-reduced-motion: reduce)" in styles.text


async def test_article_revision_query_previews_an_exact_immutable_revision(
    api_client, acceptance_database: str
) -> None:
    client, _ = api_client
    created = await client.post(
        "/api/v1/runs", json=brief(), headers={"Idempotency-Key": "revision-preview"}
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    await run_one_fixture_job(acceptance_database)

    generated = (await client.get(f"/api/v1/runs/{run_id}/article")).json()
    cited = next(claim for claim in generated["claims"] if claim["excerpt_ids"])
    params = {"excerpt": cited["excerpt_ids"][0]}
    evidence_path = f"/api/v1/runs/{run_id}/revisions/{generated['id']}/evidence"
    evidence = await client.get(evidence_path, params=params)
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["excerpt"]["content_hash"] == cited["excerpt_hashes"][0]
    assert evidence.json()["excerpt"]["source_id"] == evidence.json()["source"]["id"]
    assert evidence.json()["excerpt"]["text"]
    assert (await client.get(evidence_path, params={"excerpt": "excerpt_unlinked"})).status_code == 404
    async with database_pool(acceptance_database) as pool:
        repository = Repository(pool, acceptance_database)
        with pytest.raises(StorageError) as other_owner:
            await repository.get_cited_evidence("other-owner", run_id, generated["id"], params["excerpt"])
        assert other_owner.value.status_code == 404
    edited = await client.post(
        f"/api/v1/runs/{run_id}/revisions",
        json={"parent_revision": generated["id"], "markdown": "# Edited\n\nMy own draft."},
    )
    assert edited.status_code == 201, edited.text
    human = edited.json()
    assert (await client.get(
        f"/api/v1/runs/{run_id}/revisions/{human['id']}/evidence", params=params,
    )).status_code == 404
    assert (await client.get(evidence_path, params=params)).json() == evidence.json()

    latest = (await client.get(f"/api/v1/runs/{run_id}/article")).json()
    assert latest["id"] == human["id"]
    assert latest["review_status"] == "unassessed"

    original = await client.get(
        f"/api/v1/runs/{run_id}/article?revision={generated['id']}"
    )
    assert original.status_code == 200
    assert original.json()["id"] == generated["id"]
    assert original.json()["review_status"] == generated["review_status"]
    assert original.json()["content_hash"] == generated["content_hash"]

    saved = await client.get(f"/api/v1/runs/{run_id}/article?revision={human['id']}")
    assert saved.status_code == 200
    assert saved.json()["markdown"] == "# Edited\n\nMy own draft."

    missing = await client.get(
        f"/api/v1/runs/{run_id}/article?revision={uuid4()}"
    )
    assert missing.status_code == 404
    malformed = await client.get(f"/api/v1/runs/{run_id}/article?revision=not-a-uuid")
    assert malformed.status_code == 422
