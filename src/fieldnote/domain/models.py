"""Validated, JSON-compatible contracts for Fieldnote's trust boundaries."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from fieldnote.domain.languages import ArticleLanguage
from fieldnote.domain.research import factual_research_reason, is_general_or_creative

Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")]
Hash = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=240)]
ResearchMode = Literal["required", "auto", "off"]
RunState = Literal[
    "queued", "running", "retry_wait", "cancel_requested",
    "succeeded", "needs_review", "failed", "cancelled",
]
ReviewStatus = Literal["unassessed", "approved", "needs_review", "failed"]
TERMINAL_STATES = frozenset({"succeeded", "needs_review", "failed", "cancelled"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def markdown_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def excerpt_identity(source_id: str, content_hash: str, locator: str) -> str:
    """Identify new evidence by source, exact content and location.

    Existing persisted IDs remain valid; this does not rewrite historical rows.
    """
    import json

    identity = json.dumps([source_id, content_hash, locator], ensure_ascii=False, separators=(",", ":"))
    return "excerpt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC)


def _http_url(value: str) -> str:
    parsed = urlsplit(value)
    # Accessing the port validates its numeric form/range without making a
    # network request. Public-address policy remains in the research gateway.
    _ = parsed.port
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An HTTP(S) URL without embedded credentials is required")
    if len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("URL exceeds its permitted bounds")
    return value


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True,
        hide_input_in_errors=True, validate_default=True,
    )


class CreateRunRequest(DomainModel):
    topic: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
    audience: ShortText = "General readers"
    tone: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)] = "clear"
    language: ArticleLanguage = "en"
    target_words: Annotated[int, Field(strict=True, ge=200, le=3000)] = 1200
    research_mode: ResearchMode = "required"
    instructions: Annotated[str, StringConstraints(strict=True, max_length=4000)] = ""
    editorial_profile: Literal["standard", "technical-v1"] = "standard"
    angle: Annotated[str, StringConstraints(strict=True, max_length=1000)] = ""
    author_notes: Annotated[str, StringConstraints(strict=True, max_length=4000)] = ""
    reference_urls: Annotated[tuple[str, ...], Field(max_length=4)] = ()
    destinations: Annotated[tuple[Literal["medium", "linkedin", "x"], ...], Field(min_length=1, max_length=3)] = ("medium", "linkedin", "x")

    @field_validator("destinations")
    @classmethod
    def unique_destinations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    @field_validator("reference_urls")
    @classmethod
    def public_references(cls, urls: tuple[str, ...]) -> tuple[str, ...]:
        from fieldnote.services.urls import canonicalize_url

        return tuple(dict.fromkeys(canonicalize_url(url) for url in urls))

    def research_context(self) -> str:
        return " ".join((self.instructions, self.angle, self.author_notes, *self.reference_urls))

    def persistence_payload(self) -> dict[str, Any]:
        """Preserve pre-profile request hashes for unchanged legacy briefs."""
        data = self.model_dump(mode="json")
        for name, default in (("editorial_profile", "standard"), ("angle", ""),
                              ("author_notes", ""), ("reference_urls", []),
                              ("destinations", ["medium", "linkedin", "x"])):
            if data[name] == default:
                del data[name]
        return data

    @model_validator(mode="after")
    def research_off_boundary(self) -> CreateRunRequest:
        if self.research_mode == "off":
            context = self.research_context()
            reason = factual_research_reason(self.topic, context)
            if self.reference_urls or reason or not is_general_or_creative(self.topic, context):
                raise ValueError(
                    "research_required: research off is limited to clear creative or "
                    "general explanatory briefs without current, numeric, named-product "
                    "or source-dependent claims"
                )
        return self

    def request_hash(self) -> str:
        import json

        canonical = json.dumps(self.persistence_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResearchDecision(DomainModel):
    need_research: Annotated[bool, Field(strict=True)]
    effective_mode: Literal["required", "off"]
    reason: ShortText
    queries: Annotated[tuple[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)], ...], Field(max_length=4)] = ()

    @model_validator(mode="after")
    def consistent_decision(self) -> ResearchDecision:
        if self.need_research != (self.effective_mode == "required"):
            raise ValueError("Research mode and research decision disagree")
        if not self.need_research and self.queries:
            raise ValueError("Unresearched drafting cannot dispatch search queries")
        return self


class SectionTask(DomainModel):
    id: Identifier
    order: Annotated[int, Field(strict=True, ge=1, le=8)]
    title: ShortText
    goal: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]
    target_words: Annotated[int, Field(strict=True, ge=50, le=2500)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=24)] = ()


class ContentPlan(DomainModel):
    title: ShortText
    headline_options: Annotated[tuple[ShortText, ...], Field(max_length=5)] = ()
    audience: ShortText = "General readers"
    tone: ShortText = "clear"
    target_words: Annotated[int, Field(strict=True, ge=200, le=3000)]
    tasks: Annotated[tuple[SectionTask, ...], Field(min_length=2, max_length=8)]

    @field_validator("headline_options")
    @classmethod
    def one_line_headlines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any("\n" in value or "\r" in value for value in values):
            raise ValueError("Each headline must be one line")
        return tuple(dict.fromkeys(values))

    @model_validator(mode="after")
    def complete_ordered_budget(self) -> ContentPlan:
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("Section IDs must be unique")
        if sorted(task.order for task in self.tasks) != list(range(1, len(self.tasks) + 1)):
            raise ValueError("Section order must be unique and contiguous from one")
        if sum(task.target_words for task in self.tasks) != self.target_words:
            raise ValueError("Section word budgets must sum to the article target")
        return self


class SectionDraft(DomainModel):
    task_id: Identifier
    title: ShortText
    content_markdown: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=100_000)]
    revision: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    run_id: UUID | None = None
    cited_source_ids: Annotated[tuple[Identifier, ...], Field(max_length=40)] = ()
    generation_fingerprint: Hash | None = None
    status: Literal["draft", "accepted", "rejected"] = "draft"

    @field_validator("content_markdown")
    @classmethod
    def nonblank_markdown(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Section Markdown cannot be blank")
        return value


class Source(DomainModel):
    id: Identifier
    url: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
    title: ShortText
    retrieved_at: datetime
    content_hash: Hash
    provider: ShortText
    extraction_state: Literal["extracted", "snippet", "failed"] = "extracted"
    original_url: str | None = None
    final_url: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    origin_last_modified_at: datetime | None = None
    origin_etag: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)] | None = None
    revalidated_at: datetime | None = None

    @field_validator("url", "original_url", "final_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return None if value is None else _http_url(value)

    @field_validator("retrieved_at", "published_at", "updated_at", "origin_last_modified_at", "revalidated_at")
    @classmethod
    def utc_dates(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


class EvidenceExcerpt(DomainModel):
    id: Identifier
    source_id: Identifier
    text: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=20_000)]
    locator: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    content_hash: Hash
    source_kind: Literal["body", "snippet"] = "body"
    flags: Annotated[tuple[ShortText, ...], Field(max_length=16)] = ()


class ReviewIssue(DomainModel):
    code: Identifier
    severity: Literal["info", "warning", "error", "critical"]
    explanation: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
    task_id: Identifier | None = None
    claim_locator: Annotated[str, StringConstraints(strict=True, max_length=500)] | None = None
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=24)] = ()
    action: Annotated[str, StringConstraints(strict=True, max_length=1000)] = ""


class ClaimCitation(DomainModel):
    article_revision_id: UUID
    article_hash: Hash
    claim_locator: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    claim_text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
    excerpt_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=24)]
    excerpt_hashes: Annotated[tuple[Hash, ...], Field(min_length=1, max_length=24)]
    assessment: Literal["unassessed", "supported", "unsupported", "conflicting"] = "unassessed"
    assessor: ShortText = "unassessed"
    assessor_version: ShortText = "1"

    @model_validator(mode="after")
    def matching_excerpts(self) -> ClaimCitation:
        if len(self.excerpt_ids) != len(self.excerpt_hashes):
            raise ValueError("Every cited excerpt needs its matching hash")
        if len(set(self.excerpt_ids)) != len(self.excerpt_ids):
            raise ValueError("Cited excerpt IDs must be unique")
        return self


class EditorialReview(DomainModel):
    approved: Annotated[bool, Field(strict=True)]
    issues: Annotated[tuple[ReviewIssue, ...], Field(max_length=64)] = ()
    assessed_revision: Annotated[int, Field(strict=True, ge=0)] = 0
    assessed_hash: Hash | None = None
    reviewer: ShortText = "fixture"
    reviewer_version: ShortText = "1"

    @model_validator(mode="after")
    def approval_has_no_errors(self) -> EditorialReview:
        if self.approved and any(issue.severity in {"error", "critical"} for issue in self.issues):
            raise ValueError("A revision with unresolved errors cannot be approved")
        return self


class ArticleRevision(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    revision: Annotated[int, Field(strict=True, ge=0)] = 0
    title: ShortText
    content_markdown: Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=200_000)]
    content_hash: Hash
    review_status: ReviewStatus = "unassessed"
    parent_revision_id: UUID | None = None
    author_type: Literal["generated", "human"] = "generated"
    created_at: datetime = Field(default_factory=utc_now)
    cited_source_ids: Annotated[tuple[Identifier, ...], Field(max_length=80)] = ()
    issues: Annotated[tuple[ReviewIssue, ...], Field(max_length=64)] = ()

    @field_validator("created_at")
    @classmethod
    def utc_created(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def revision_integrity(self) -> ArticleRevision:
        if not self.content_markdown.strip():
            raise ValueError("Article Markdown cannot be blank")
        if markdown_hash(self.content_markdown) != self.content_hash:
            raise ValueError("Article content hash does not match its UTF-8 Markdown")
        if self.author_type == "human" and self.review_status != "unassessed":
            raise ValueError("Human edits do not inherit generated approval")
        if self.review_status == "approved" and any(issue.severity in {"error", "critical"} for issue in self.issues):
            raise ValueError("Unresolved errors cannot carry approved status")
        return self


class Run(DomainModel):
    """Persisted run identity, distinct from its selected revision's review."""

    id: UUID = Field(default_factory=uuid4)
    owner_id: ShortText
    request: CreateRunRequest
    request_hash: Hash
    policy: ResearchDecision
    state: RunState = "queued"
    created_at: datetime = Field(default_factory=utc_now)
    deadline_at: datetime
    updated_at: datetime = Field(default_factory=utc_now)
    graph_version: ShortText = "1"
    schema_version: ShortText = "1"
    prompt_version: ShortText = "1"
    safe_error_code: Identifier | None = None
    selected_revision_id: UUID | None = None

    @field_validator("created_at", "deadline_at", "updated_at")
    @classmethod
    def utc_timestamps(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def valid_run(self) -> Run:
        if self.request_hash != self.request.request_hash():
            raise ValueError("Run hash does not match its normalized brief")
        if self.deadline_at <= self.created_at:
            raise ValueError("Run deadline must follow acceptance")
        if self.updated_at < self.created_at:
            raise ValueError("Run update cannot precede acceptance")
        return self


class RunEvent(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    sequence: Annotated[int, Field(strict=True, ge=1)]
    event_type: Identifier
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def utc_created(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("payload")
    @classmethod
    def bounded_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Event payload must contain JSON primitives") from exc
        if len(raw.encode("utf-8")) > 16_384:
            raise ValueError("Event payload exceeds 16 KiB")
        return value
