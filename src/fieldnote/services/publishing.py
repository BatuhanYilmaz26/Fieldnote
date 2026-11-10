"""Local, extractive publication drafts and safe downloadable text bundles.

No model calls or social credentials are involved. Extracts need editorial
review: a length-valid social post is not automatically factually approved.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from datetime import UTC, datetime

from fieldnote._vendor.twitter_text import parse_tweet
from fieldnote.domain.models import markdown_hash
from fieldnote.domain.publishing import PublicationRequest
from fieldnote.services.discovery import article_discovery, plain_inline
from fieldnote.services.markdown import markdown_html
from fieldnote.services.technical import source_kind

DISCLOSURES = {
    "en": "AI assistance was used to prepare this draft.",
    "tr": "Bu taslak hazırlanırken yapay zekâ desteği kullanılmıştır.",
    "es": "Se utilizó asistencia de IA para preparar este borrador.",
    "pt": "Foi utilizada assistência de IA para preparar este rascunho.",
    "de": "Dieser Entwurf wurde mit KI-Unterstützung vorbereitet.",
}
MEDIUM_POLICY = "https://help.medium.com/hc/en-us/articles/22576852947223-Artificial-Intelligence-AI-content-policy"
CAVEAT = re.compile(r"\b(however|limitation|caveat|tradeoff|trade-off|caution|not guaranteed|depends|"
                    r"ancak|sınırl|garanti|sin embargo|limitaci|ressalva|limitaç|jedoch|einschränk)", re.I)


def x_count(text: str) -> dict:
    result = parse_tweet(text)
    return {"weighted_length": result.weightedLength, "limit": 280, "valid": result.valid,
            "counter": "twitter-text-v3/vendored-3.0.0"}


def linkedin_count(text: str) -> int:
    # Conservative UTF-16 units, including spaces/newlines, for the published
    # 3,000-character cap. Never undercount supplementary emoji as one unit.
    return len(text.encode("utf-16-le")) // 2


def plain_markdown(markdown: str) -> str:
    lines = []
    in_code = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append(line)  # Code operators/spacing must remain literal.
            continue
        line = re.sub(r"\[([^\]]+)\]\(<?(https?://[^)>]+)>?\)", r"\1 (\2)", line)
        line = re.sub(r"^\s{0,3}#{1,6}\s+|^>\s?", "", line)
        lines.append(plain_inline(line))
    return "\n".join(lines).strip() + "\n"


def article_html(markdown: str, title: str) -> str:
    """An escaped reading/copy artifact, never executable authored HTML."""
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'none\'; base-uri \'none\'">'
            "<title>" + html.escape(title) + "</title></head><body>" + markdown_html(markdown) + "</body></html>")


def article_body(markdown: str) -> str:
    """Remove only the first prose H1 for editors with a separate title field."""
    lines = markdown.splitlines(keepends=True)
    in_code = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"^ {0,3}#\s+", line):
            return "".join(lines[:index] + lines[index + 1:]).strip()
    return markdown.strip()


def _paragraphs(raw: str) -> list[str]:
    # Do not turn code, headings or partial lines into factual social claims.
    text = re.sub(r"(?m)^[^\n]{1,100}:\n\n?(?=```)", "", raw)
    text = re.sub(r"```[\s\S]*?(?:```|$)", "", text)
    text = re.sub(r"\[@[^\]]+\]", "", text)
    return [plain_inline(p) for p in text.split("\n\n")
            if p.strip() and not re.match(r"^\s*(?:#|>|\||[-*]\s|\d+[.)]\s)", p)]


def _extract_linkedin(paragraphs: list[str], title: str, link: str) -> str:
    chosen: list[str] = []
    for paragraph in paragraphs:
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            if sum(len(part.split()) for part in [title, *chosen, sentence]) <= 180:
                chosen.append(sentence)
            if sum(len(part.split()) for part in chosen) >= 130:
                break
        if sum(len(part.split()) for part in chosen) >= 130:
            break
    caveat = next((p for p in paragraphs if CAVEAT.search(p) and p not in chosen), None)
    if caveat and len(caveat.split()) <= 55:
        # Keep a caveat together without repeating sentences already selected
        # from that same paragraph.
        chosen = [sentence for sentence in chosen if sentence not in caveat] + [caveat]
    result = "\n\n".join([title, *dict.fromkeys(chosen), *([link] if link else [])])
    while linkedin_count(result) > 3000 and chosen:
        chosen.pop()
        result = "\n\n".join([title, *chosen, *([link] if link else [])])
    return result


def _extract_x(paragraphs: list[str], title: str, link: str, length: int) -> list[str]:
    # Whole sentences only. A long sentence is omitted, never cut mid-claim.
    sentences = list(dict.fromkeys([s.strip() for p in paragraphs for s in re.split(r"(?<=[.!?])\s+", p) if s.strip()]))
    candidates = []
    for sentence in sentences:
        if len(sentence) <= 2000 and x_count(sentence)["weighted_length"] <= 230:
            candidates.append(sentence)
        if len(candidates) >= 20:
            break
    if not candidates:
        candidates = [title]
    count = min(length, len(candidates))
    results = []
    for index, sentence in enumerate(candidates[:count]):
        prefix = f"{index + 1}/{count} " if count > 1 else ""
        suffix = f"\n\n{link}" if link and index == count - 1 else ""
        text = prefix + sentence + suffix
        if not x_count(text)["valid"]:
            text = prefix + title + suffix
        results.append(text)
    return results


def build_publication(article: dict, request: PublicationRequest, brief: dict) -> dict:
    if request.content_hash != article["content_hash"] or markdown_hash(article["markdown"]) != article["content_hash"]:
        raise ValueError("revision_conflict")
    citation_map = article.get("citation_map", {})
    sources = citation_map.get("sources", {})
    raw = citation_map.get("editable_markdown", article["markdown"])
    language = brief.get("language", "en")
    discovery = article_discovery(raw)
    discovery.pop("headline_options")  # Unselected planning ideas stay in the private workspace.
    title_text = discovery["title"] or str(brief.get("topic", "Technical article"))[:240]
    discovery.update({"search_title": request.search_title or title_text,
                      "search_description": request.search_description or discovery["description_suggestion"],
                      "revision_id": article["id"], "content_hash": article["content_hash"]})
    references = [{key: source.get(key) for key in ("id", "title", "url", "published_at", "updated_at", "retrieved_at", "content_hash", "provider")}
                  for source in sources.values()]
    for reference in references:
        reference["source_kind"] = source_kind(reference["url"])
        reference["event_at"] = None
    link = request.canonical_url or (references[0]["url"] if references else "")
    paragraphs = _paragraphs(raw)
    linkedin = request.linkedin_text if request.linkedin_text is not None else _extract_linkedin(paragraphs, title_text, link)
    posts = list(request.x_posts) if request.x_posts is not None else _extract_x(paragraphs, title_text, link, request.thread_length)
    counts = [x_count(post) for post in posts]
    destinations = brief.get("destinations", ["medium", "linkedin", "x"])
    if ("linkedin" in destinations and linkedin_count(linkedin) > 3000) or ("x" in destinations and any(not count["valid"] for count in counts)):
        raise ValueError("platform_length_invalid")
    synthetic = any(str(s.get("provider", "")).startswith("fixture") for s in sources.values()) or "synthetic_fixture" in str(article.get("issues", []))
    disclosure = request.disclosure or DISCLOSURES[language]
    medium = disclosure + "\n\n" + article["markdown"]
    medium_body = disclosure + "\n\n" + article_body(article["markdown"])
    warnings = [
        "Review social extracts for missing caveats and context; length validation is not factual approval.",
        "Medium requires disclosure for generated text. AI-generated writing cannot be paywalled in its Partner Program.",
    ]
    if synthetic:
        warnings.append("Synthetic fixture material is not ready for publication.")
    assessment = article.get("assessment")
    status = assessment["review_status"] if assessment else article.get("review_status", "unassessed")
    if status != "approved":
        warnings.append("The exact article revision has not passed editorial assessment.")
    if not 120 <= len(linkedin.split()) <= 200:
        warnings.append("LinkedIn is outside the suggested 120–200 word range; adjust it without adding unsupported facts.")
    if request.thread_length > 1 and len(posts) != request.thread_length:
        warnings.append("There were too few short source sentences for the requested thread; edit the post list yourself.")
    checklist = {key: getattr(request, key) for key in ("facts_checked", "voice_checked", "language_checked", "variants_checked", "disclosure_checked")}
    return {
        "schema_version": 2, "revision_id": article["id"], "content_hash": article["content_hash"],
        "language": language, "editorial_profile": brief.get("editorial_profile", "standard"),
        "title": title_text, "created_at": datetime.now(UTC).isoformat(),
        "discovery": discovery,
        "destinations": brief.get("destinations", ["medium", "linkedin", "x"]),
        "canonical_url": request.canonical_url, "disclosure": disclosure,
        "article_markdown": article["markdown"], "medium_markdown": medium,
        "article_text": plain_markdown(medium), "article_html": article_html(medium, title_text),
        "medium_body_text": plain_markdown(medium_body), "medium_body_html": markdown_html(medium_body),
        "linkedin_text": linkedin, "x_posts": posts,
        "counts": {"linkedin": linkedin_count(linkedin), "x": counts},
        "references": references, "warnings": warnings, "checklist": checklist,
        "article_review_status": status, "synthetic": synthetic,
        "assessment_review_id": assessment.get("review_run_id") if assessment else None,
        "ready": all(checklist.values()) and status == "approved" and not synthetic,
        "method": "extractive-v2; author-edited variants require independent review",
        "policy_url": MEDIUM_POLICY,
    }


def publication_zip(package: dict) -> bytes:
    """Fixed filenames, explicit safe fields; never includes private brief notes."""
    data = package["data"]
    files = {"article-original.md": data["article_markdown"], "medium.md": data["medium_markdown"],
             "article.txt": data["article_text"], "article.html": data["article_html"],
             "references.json": json.dumps(data["references"], ensure_ascii=False, indent=2)}
    if "discovery" in data:  # Older immutable packages retain their original contents.
        files["discovery.json"] = json.dumps(data["discovery"], ensure_ascii=False, indent=2)
    if "medium_body_html" in data:
        files.update({"title.txt": data["title"], "medium-body.html": data["medium_body_html"],
                      "medium-body.txt": data["medium_body_text"]})
    if "linkedin" in data["destinations"]:
        files["linkedin.txt"] = data["linkedin_text"]
    if "x" in data["destinations"]:
        files.update({f"x-{i}.txt": post for i, post in enumerate(data["x_posts"], 1)})
    files["HANDOFF.txt"] = (
        "Review every claim, example, disclosure and platform variant before posting manually.\n"
        "article-original.md preserves the saved article bytes; medium.md adds the disclosure.\n"
        "Keep the disclosure near the beginning. Set your canonical URL in Medium if cross-posting.\n"
        + ("title.txt is for the title field; medium-body.html/txt omit the first article H1.\n"
           "Use the HTML body for formatted pasting where supported; inspect links, lists and code afterward.\n"
           if "medium_body_html" in data else "")
        + ("discovery.json contains search metadata suggestions, not platform settings or factual approval.\n"
           "Set title/description in your destination editor where supported, then inspect the preview.\n"
           "Check headings, lists and code formatting after pasting into Medium. No ranking or AI citation is guaranteed.\n"
           if "discovery" in data else "")
        + "No content has been posted by Fieldnote. Record your published URLs in the workspace.\n"
        + ("WARNING: this package belongs to an older article revision.\n" if package.get("stale") else "")
        + "\n".join(data["warnings"]) + "\n" + MEDIUM_POLICY + "\n"
    )
    manifest = {key: data[key] for key in ("schema_version", "revision_id", "content_hash", "language", "editorial_profile", "canonical_url", "disclosure", "method", "article_review_status", "synthetic", "checklist")}
    manifest.update({"package_id": package["id"], "created_at": package["created_at"],
                     "stale": package.get("stale", False), "ready": data["ready"] and not package.get("stale"),
                     "files": {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in files.items()}})
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text.encode("utf-8"))
    return output.getvalue()
