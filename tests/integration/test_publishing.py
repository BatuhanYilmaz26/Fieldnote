"""Publication packages exercise real ownership, immutable history and staleness."""

import io
import json
import zipfile
from datetime import UTC, datetime

import pytest
from tests.integration.test_api import enqueue, run_one_fixture_job

pytestmark = pytest.mark.asyncio


async def test_new_handoff_schema_does_not_reuse_or_rewrite_old_package(api_client, acceptance_database):
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from fieldnote.domain.publishing import PublicationRequest
    from fieldnote.storage.repository import canonical_hash

    client, app = api_client
    run_id = (await enqueue(client, key="handoff-upgrade"))["id"]
    await run_one_fixture_job(acceptance_database)
    base = f"/api/v1/runs/{run_id}"
    article = (await client.get(base + "/article")).json()
    route = base + f"/revisions/{article['id']}/packages"
    payload = {"content_hash": article["content_hash"]}
    initial = (await client.post(route, json=payload)).json()
    old = dict(initial["data"])
    old.pop("medium_body_html")
    old.pop("medium_body_text")
    old["schema_version"] = 1
    # Represent a version-one row's old deduplication identity. Only test data
    # is seeded; production code must never rewrite immutable package contents.
    old_id = str(uuid4())
    old_digest = canonical_hash({"inputs": PublicationRequest(**payload).model_dump(mode="json"),
        "assessment_review_id": old.get("assessment_review_id"), "article_review_status": old["article_review_status"]})
    async with app.state.repository.pool.connection() as connection:
        await connection.execute("INSERT INTO publication_packages(id,run_id,revision_id,content_hash,input_hash,data) VALUES (%s,%s,%s,%s,%s,%s)",
            (old_id, run_id, article["id"], article["content_hash"], old_digest, Jsonb(old)))
        await connection.commit()
    newer = (await client.post(route, json=payload)).json()
    assert newer["id"] != old_id and newer["data"]["schema_version"] == 2
    assert "medium_body_html" in newer["data"]
    assert (await client.post(route, json=payload)).json()["id"] == newer["id"]
    retained = (await client.get(base + f"/packages/{old_id}")).json()
    assert retained["data"] == old


async def test_prepare_edit_export_record_and_stale_package(api_client, acceptance_database):
    client, app = api_client
    run_id = (await enqueue(client, key="publishing", author_notes="PRIVATE_NOTES_8412"))["id"]
    await run_one_fixture_job(acceptance_database)
    base = f"/api/v1/runs/{run_id}"
    article = (await client.get(base + "/article")).json()
    route = base + f"/revisions/{article['id']}/packages"
    payload = {"content_hash": article["content_hash"]}
    response = await client.post(route, json=payload)
    assert response.status_code == 201, response.text
    package = response.json()
    assert not package["stale"] and not package["data"]["ready"] and package["data"]["synthetic"]
    duplicate = await client.post(route, json=payload)
    assert duplicate.json()["id"] == package["id"]
    changed = await client.post(route, json={**payload, "linkedin_text": "My independently reviewed technical post.", "x_posts": ["An edited short technical observation."], "canonical_url": "https://example.org/original"})
    assert changed.status_code == 201
    assert changed.json()["id"] != package["id"]
    assert changed.json()["data"]["linkedin_text"] == "My independently reviewed technical post."
    assert not changed.json()["data"]["ready"]
    invalid = await client.post(route, json={**payload, "x_posts": ["x" * 281]})
    assert invalid.status_code == 422
    unicode_payload = json.dumps({**payload, "x_posts": ["👨‍👩‍👧‍👦" * 140] * 5})
    assert len(unicode_payload) > 32_768
    unicode_package = await client.post(route, content=unicode_payload, headers={"Content-Type": "application/json"})
    assert unicode_package.status_code == 201, unicode_package.text
    assert all(p["weighted_length"] == 280 for p in unicode_package.json()["data"]["counts"]["x"])
    assert (await client.post(route, json={"content_hash": "a" * 64})).status_code == 409
    download = await client.get(base + f"/packages/{package['id']}/download")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert archive.read("article-original.md").decode() == article["markdown"]
    assert all(b"PRIVATE_NOTES_8412" not in archive.read(name) for name in archive.namelist())
    record = {"platform": "medium", "url": "https://medium.com/@example/a-technical-post", "published_at": datetime.now(UTC).isoformat()}
    record_route = base + f"/packages/{package['id']}/publications"
    logged = await client.post(record_route, json=record)
    assert logged.status_code == 201
    assert (await client.post(record_route, json=record)).json()["id"] == logged.json()["id"]
    assert len((await client.get(base + "/publications")).json()["publications"]) == 1
    assert (await client.post(record_route, json={**record, "published_at": "2099-01-01T00:00:00Z"})).status_code == 422
    assert (await client.post(route, json=payload, headers={"X-CSRF-Token": ""})).status_code == 403
    edited = await client.post(base + "/revisions", json={"parent_revision": article["id"], "markdown": "# Changed\n\nA new saved article."})
    assert edited.status_code == 201
    assert (await client.get(base + f"/packages/{package['id']}")).json()["stale"]
    assert (await client.post(route, json=payload)).status_code == 409
    stale_zip = zipfile.ZipFile(io.BytesIO((await client.get(base + f"/packages/{package['id']}/download")).content))
    assert json.loads(stale_zip.read("manifest.json"))["stale"]
    app.state.settings.local_owner_id = "another-publisher"
    session = await client.post("/api/v1/session")
    client.headers["X-CSRF-Token"] = session.json()["csrf_token"]
    for path in (base + "/packages", base + f"/packages/{package['id']}", base + f"/packages/{package['id']}/download", base + "/publications"):
        assert (await client.get(path)).status_code == 404
    assert (await client.post(record_route, json=record)).status_code == 404
    assert (await client.post(route, json=payload)).status_code == 404


async def test_new_assessment_marks_old_package_stale_without_reusing_approval(api_client, acceptance_database):
    client, _ = api_client
    run_id = (await enqueue(client, key="package-assessment"))["id"]
    await run_one_fixture_job(acceptance_database)
    base = f"/api/v1/runs/{run_id}"
    article = (await client.get(base + "/article")).json()
    edited = (await client.post(base + "/revisions", json={"parent_revision": article["id"], "markdown": "# Edited\n\n" + "An illustrative technical explanation. " * 100})).json()
    route = base + f"/revisions/{edited['id']}/packages"
    inputs = {"content_hash": edited["content_hash"]}
    first = await client.post(route, json=inputs)
    assert first.status_code == 201
    review = await client.post(base + f"/revisions/{edited['id']}/reviews", json=inputs, headers={"Idempotency-Key": "package-review"})
    assert review.status_code == 202
    await run_one_fixture_job(acceptance_database)
    old = (await client.get(base + f"/packages/{first.json()['id']}")).json()
    assert old["stale"] and old["review_changed"]
    fresh = await client.post(route, json=inputs)
    assert fresh.status_code == 201 and fresh.json()["id"] != old["id"]
    assert not fresh.json()["stale"] and not fresh.json()["data"]["ready"]


@pytest.mark.parametrize("case_index", range(12))
async def test_technical_corpus_to_text_package_through_real_worker(api_client, acceptance_database, case_index):
    from pathlib import Path

    corpus = Path(__file__).resolve().parents[2] / "evaluation/corpus/v2/briefs.jsonl"
    cases = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
    brief = [case for case in cases if case["category"] == "technical_publishing"][case_index]["request"]
    client, _ = api_client
    created = await client.post("/api/v1/runs", json=brief, headers={"Idempotency-Key": "technical-corpus-package"})
    assert created.status_code == 202
    run_id = created.json()["id"]
    await run_one_fixture_job(acceptance_database)
    base = f"/api/v1/runs/{run_id}"
    article = (await client.get(base + "/article")).json()
    assert article["review_status"] == "needs_review"
    prepared = await client.post(base + f"/revisions/{article['id']}/packages", json={"content_hash": article["content_hash"]})
    assert prepared.status_code == 201, prepared.text
    package = prepared.json()
    assert not package["data"]["ready"] and package["data"]["synthetic"]
    assert all(p["valid"] for p in package["data"]["counts"]["x"])
    download = await client.get(base + f"/packages/{package['id']}/download")
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert archive.read("article-original.md").decode() == article["markdown"]
    assert json.loads(archive.read("manifest.json"))["content_hash"] == article["content_hash"]


@pytest.mark.parametrize("language", ["en", "tr", "es", "pt", "de"])
async def test_five_language_articles_keep_exact_bytes_and_localized_disclosures(api_client, acceptance_database, language):
    from fieldnote.domain.languages import language_profile
    from fieldnote.services.publishing import DISCLOSURES

    client, _ = api_client
    created = await client.post("/api/v1/runs", json={"topic": "A technical explanation", "language": language,
        "target_words": 350, "editorial_profile": "technical-v1"}, headers={"Idempotency-Key": "localized-package"})
    assert created.status_code == 202
    run_id = created.json()["id"]
    await run_one_fixture_job(acceptance_database)
    base = f"/api/v1/runs/{run_id}"
    article = (await client.get(base + "/article")).json()
    result = await client.post(base + f"/revisions/{article['id']}/packages", json={"content_hash": article["content_hash"]})
    assert result.status_code == 201
    package = result.json()
    assert package["data"]["language"] == language
    assert package["data"]["disclosure"] == DISCLOSURES[language]
    assert language_profile(language).synthetic_notice in package["data"]["article_markdown"]
    assert not package["data"]["ready"]
    archive = zipfile.ZipFile(io.BytesIO((await client.get(base + f"/packages/{package['id']}/download")).content))
    assert archive.read("article-original.md").decode() == article["markdown"]
    assert archive.read("medium.md").decode().startswith(DISCLOSURES[language])


async def test_title_options_metadata_and_reading_notes_follow_saved_revision(api_client, acceptance_database):
    client, _ = api_client
    run_id = (await enqueue(client, key="discovery", editorial_profile="technical-v1", topic="RAG evaluation", target_words=350))["id"]
    await run_one_fixture_job(acceptance_database)
    base = f"/api/v1/runs/{run_id}"
    generated = (await client.get(base + "/article")).json()
    assert generated["discovery"]["headline_options"] == ["RAG evaluation"]
    assert generated["discovery"]["title"] == "RAG evaluation"
    changed = await client.post(base + "/revisions", json={"parent_revision": generated["id"], "markdown": "# Why RAG similarity misses answer quality\n\nEvaluate the answer against its evidence.\n\n## A practical check\n\nKeep the limitation beside the claim."})
    assert changed.status_code == 201, changed.text
    saved = (await client.get(base + "/article")).json()
    assert saved["id"] != generated["id"] and saved["review_status"] == "unassessed"
    assert saved["discovery"]["title"] == "Why RAG similarity misses answer quality"
    assert saved["discovery"]["headline_options"] == generated["discovery"]["headline_options"]
    route = base + f"/revisions/{saved['id']}/packages"
    payload = {"content_hash": saved["content_hash"], "search_title": "RAG evaluation: evidence before similarity", "search_description": "A concrete answer-quality check and its limitation."}
    first = (await client.post(route, json=payload)).json()
    assert first["data"]["discovery"]["search_title"] == payload["search_title"]
    second = (await client.post(route, json={**payload, "search_description": "A revised accurate summary."})).json()
    assert second["id"] != first["id"]
    old = (await client.get(base + f"/packages/{first['id']}")).json()
    assert old["data"]["discovery"]["search_description"] == payload["search_description"]
    archive = zipfile.ZipFile(io.BytesIO((await client.get(base + f"/packages/{second['id']}/download")).content))
    metadata = json.loads(archive.read("discovery.json"))
    assert metadata["content_hash"] == saved["content_hash"] and metadata["revision_id"] == saved["id"]
    original = (await client.get(base + f"/article?revision={generated['id']}")).json()
    assert original["discovery"]["title"] == "RAG evaluation"
