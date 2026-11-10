"""Exact bytes, safe exports and upstream v3 weighted-character examples."""

import hashlib
import io
import json
import zipfile
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fieldnote.domain import markdown_hash
from fieldnote.domain.publishing import PublicationRequest
from fieldnote.services.publishing import (
    article_body,
    article_html,
    build_publication,
    publication_zip,
    x_count,
)


@pytest.mark.parametrize("text,expected,valid", [
    ("a" * 280, 280, True), ("a" * 281, 281, False),
    ("界" * 140, 280, True), ("界" * 141, 282, False),
    ("👨‍👩‍👧‍👦" * 140, 280, True), ("🙋🏽", 2, True), ("1️⃣", 2, True),
    ("e\u0301", 1, True), ("https://example.com/" + "a" * 500, 23, True),
    ("english text 日本語 😷 https://example.com", 46, True),
    ("", 0, False), ("\uffff", 2, False),
])
def test_x_v3_unicode_emoji_url_and_boundary_examples(text, expected, valid):
    result = x_count(text)
    assert result["weighted_length"] == expected and result["valid"] == valid


def article(language="en"):
    body = "# A technical idea\n\n" + ("This is a useful explanation of a limited technical example. " * 13)
    body += "\n\nHowever, these observations do not establish a benchmark result.\n"
    return {"id": str(uuid4()), "markdown": body, "content_hash": markdown_hash(body),
            "review_status": "needs_review", "citation_map": {"editable_markdown": body, "language": language}, "issues": []}


@pytest.mark.parametrize("language", ["en", "tr", "es", "pt", "de"])
async def test_bundle_preserves_revision_bytes_and_excludes_private_brief_notes(language):
    item = article(language)
    request = PublicationRequest(content_hash=item["content_hash"], thread_length=3)
    data = build_publication(item, request, {"topic": "A technical idea", "language": language,
                                           "author_notes": "PRIVATE_OBSERVATION_7184"})
    assert not data["ready"] and data["article_review_status"] == "needs_review"
    assert data["medium_markdown"].endswith(item["markdown"])
    assert all(post["valid"] for post in data["counts"]["x"])
    assert data["disclosure"] in data["medium_markdown"].splitlines()[0]
    package = {"id": str(uuid4()), "data": data, "created_at": data["created_at"], "stale": True}
    archive = zipfile.ZipFile(io.BytesIO(publication_zip(package)))
    assert archive.read("article-original.md").decode() == item["markdown"]
    assert archive.read("title.txt").decode() == data["title"]
    assert archive.read("medium-body.html").decode() == data["medium_body_html"]
    assert "<h1>" not in data["medium_body_html"]
    assert data["medium_body_text"].startswith(data["disclosure"])
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["stale"] and not manifest["ready"]
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert all("PRIVATE_OBSERVATION_7184" not in archive.read(name).decode() for name in archive.namelist())


def test_package_rejects_wrong_hash_invalid_platform_lengths_and_unsafe_url():
    item = article()
    for inputs in ({"content_hash": "a" * 64}, {"content_hash": item["content_hash"], "x_posts": ["a" * 281]},
                   {"content_hash": item["content_hash"], "linkedin_text": "😀" * 1600}):
        with pytest.raises(ValueError):
            build_publication(item, PublicationRequest(**inputs), {})
    with pytest.raises(ValidationError):
        PublicationRequest(content_hash=item["content_hash"], canonical_url="http://localhost/private")
    with pytest.raises(ValidationError):
        PublicationRequest(content_hash=item["content_hash"], x_posts=["\ud800"])


def test_html_export_escapes_authored_markup_and_retains_code():
    output = article_html('# <script>bad()</script>\n\n```html\n<img src="https://example.org/a">\n```', "Unsafe <title>")
    assert "<script>" not in output and "<img " not in output
    assert "&lt;script&gt;" in output and "<pre><code>" in output
    assert "default-src 'none'" in output


def test_separate_body_keeps_code_headings_disclosure_and_later_headings():
    markdown = "```markdown\n# Code, not the title\n```\n\n# Article title\n\n## A section\n\n# A later heading\n"
    body = article_body(markdown)
    assert "# Code, not the title" in body
    assert "# Article title" not in body
    assert "## A section" in body and "# A later heading" in body
    assert article_body("No heading.\n\nKeep this body.") == "No heading.\n\nKeep this body."


def test_old_immutable_packages_export_without_new_handoff_fields():
    item = article()
    data = build_publication(item, PublicationRequest(content_hash=item["content_hash"]), {})
    data.pop("medium_body_text")
    data.pop("medium_body_html")
    data["schema_version"] = 1
    package = {"id": str(uuid4()), "data": data, "created_at": data["created_at"]}
    archive = zipfile.ZipFile(io.BytesIO(publication_zip(package)))
    assert "title.txt" not in archive.namelist()
    assert archive.read("article-original.md").decode() == item["markdown"]


def test_vendored_counter_source_matches_reviewed_manifest():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/fieldnote/_vendor/twitter_text"
    manifest = json.loads((root / "manifest.json").read_text())
    for path, hashes in manifest.items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == hashes["sha256"]
    assert (root / "LICENSE.txt").exists() and (root / "NOTICE.txt").exists()


def test_official_v3_discounted_emoji_and_directional_fixtures():
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parents[1] / "fixtures/x-v3.json").read_text())
    assert len(data["cases"]) >= 20
    for case in data["cases"]:
        result = x_count(case["text"])
        assert result["weighted_length"] == case["expected"]["weightedLength"], case["description"]
        assert result["valid"] == case["expected"]["valid"], case["description"]
