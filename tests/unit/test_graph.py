"""Execute real LangGraph dispatch, revision, checkpoints and failure gates."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from fieldnote.domain import CreateRunRequest, SectionDraft
from fieldnote.domain.languages import LANGUAGES
from fieldnote.graph import (
    GraphDependencies,
    GraphError,
    GraphHooks,
    build_graph,
    initial_state,
    make_fixture_dependencies,
    merge_sections,
)
from fieldnote.graph.workflow import _bounded_context_json
from fieldnote.providers.base import ModelResult, ModelT
from fieldnote.providers.fixture import FixtureGateway, FixtureResearchGateway


def test_prompt_previews_fit_utf8_and_leave_stored_evidence_untouched():
    long = 'Evidence with "quotes" and Türkçe.\n' * 1000
    context = {"brief": {"angle": "kept"}, "evidence": {
        "short": {"text": "Short source.", "source_id": "src1", "content_hash": "a" * 64},
        "long": {"text": long, "source_id": "src2", "content_hash": "b" * 64},
    }}
    prompt = _bounded_context_json(context, 1900)
    assert len(prompt.encode("utf-8")) <= 1900
    bounded = json.loads(prompt)
    assert bounded["brief"] == context["brief"]
    assert bounded["evidence"]["short"] == context["evidence"]["short"]
    preview = bounded["evidence"]["long"]
    assert preview["prompt_preview"] and 100 < len(preview["text"]) < len(long)
    assert long.startswith(preview["text"])
    assert preview["content_hash"] == "b" * 64
    assert context["evidence"]["long"]["text"] == long


class RecordingGateway(FixtureGateway):
    """Synthetic transport marked non-fixture solely to exercise approval edges.

    It does not qualify a live provider or factual review. The fixture helper
    itself is separately required never to imply reviewed factual success.
    """

    def __init__(self, responses: dict[str, dict[str, Any]], task_count: int) -> None:
        super().__init__(responses)
        self.finished: list[str] = []
        self.messages: dict[str, list[dict[str, str]]] = {}
        self.task_count = task_count

    async def generate(self, schema: type[ModelT], messages: list[dict[str, str]], call_identity: str, max_tokens: int = 2048, *, reserved_cost_usd: Any = None) -> ModelResult[ModelT]:
        self.messages[call_identity] = messages
        if call_identity.startswith("section:"):
            task_number = int(call_identity.split(":")[1].split("_")[1])
            await asyncio.sleep((self.task_count - task_number) * 0.002)
            self.finished.append(call_identity)
        result = await super().generate(schema, messages, call_identity, max_tokens, reserved_cost_usd=reserved_cost_usd)
        return replace(result, fixture=False)


def scripted(section_count: int = 2, rejected_rounds: int = 0) -> tuple[CreateRunRequest, RecordingGateway]:
    words = 250 if section_count == 2 else 100
    request = CreateRunRequest(topic="Write a short story about a dragon", target_words=words * section_count, research_mode="off")
    tasks = [
        {"id": f"section_{i}", "order": i, "title": f"Section {i}", "goal": "Continue the fictional story", "target_words": words}
        for i in range(1, section_count + 1)
    ]
    responses: dict[str, dict[str, Any]] = {
        "plan": {"title": "A dragon tale", "target_words": request.target_words, "tasks": tasks},
    }
    for revision in range(3):
        for task in tasks:
            responses[f"section:{task['id']}:{revision}"] = {
                "task_id": task["id"], "title": task["title"], "revision": revision,
                "content_markdown": (f"revision{revision} " + "dragon " * words).strip(),
            }
        responses[f"review:{revision}"] = {
            "approved": revision >= rejected_rounds, "assessed_revision": revision,
            "issues": [] if revision >= rejected_rounds else [{"code": "repetition", "severity": "error", "task_id": "section_1", "explanation": "Rewrite the repeated beginning."}],
        }
    return request, RecordingGateway(responses, section_count)


@pytest.mark.parametrize("section_count", [2, 8])
async def test_real_send_barrier_and_stable_assembly(section_count: int) -> None:
    request, gateway = scripted(section_count)
    graph = build_graph(GraphDependencies(model=gateway, request=request, max_concurrency=8)).compile()
    result = await graph.ainvoke(initial_state())
    assert len(result["sections"]) == section_count
    assert gateway.finished[0].startswith(f"section:section_{section_count}:")
    assert result["outcome"] == "succeeded"
    assert result["assembled_markdown"].index("## Section 1") < result["assembled_markdown"].index(f"## Section {section_count}")
    assert result["model_calls"] == section_count + 2


async def test_planner_selected_excerpts_are_bounded_before_section_dispatch():
    request, gateway = scripted()
    request = CreateRunRequest(**{**request.model_dump(), "research_mode": "required"})
    research = FixtureResearchGateway({
        f"https://example.org/page-{i}": {"title": "Dragon story", "text": "dragon story " * 460 + str(i)}
        for i in range(5)
    })
    identities = [(await research.extract(url))["excerpt"]["id"] for url in research._pages]
    for task in gateway._responses["plan"]["tasks"]:
        task["evidence_ids"] = identities
    result = await build_graph(GraphDependencies(
        model=gateway, request=request, research=research,
    )).compile().ainvoke(initial_state())
    assert len(result["excerpts"]) == 5
    for identity in ("section:section_1:0", "section:section_2:0"):
        context = json.loads(gateway.messages[identity][1]["content"])
        selected = context["excerpts"]
        assert len(selected) == 2
        assert set(context["task"]["evidence_ids"]) == set(selected)
        assert sum((len(item["text"].encode()) + 3) // 4 for item in selected.values()) <= 4000
        assert all(item == result["excerpts"][key] for key, item in selected.items())


async def test_targeted_revision_only_rewrites_issue_task() -> None:
    request, gateway = scripted(rejected_rounds=1)
    graph = build_graph(GraphDependencies(model=gateway, request=request)).compile()
    result = await graph.ainvoke(initial_state())
    assert result["outcome"] == "succeeded"
    assert set(result["sections"]) == {"section_1:0", "section_2:0", "section_1:1"}
    assert "section:section_2:1" not in gateway.calls
    assert result["assembled_markdown"].count("## Section 1") == 1
    assert "revision1" in result["assembled_markdown"]
    assert result["review"]["assessed_revision"] == 1


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_model_stage_receives_the_selected_language(language):
    request, gateway = scripted()
    request = CreateRunRequest(**{**request.model_dump(), "language": language})
    result = await build_graph(GraphDependencies(model=gateway, request=request)).compile().ainvoke(initial_state())
    assert len(gateway.calls) == 4  # No retries solely for an unqualified locale.
    for messages in gateway.messages.values():
        assert LANGUAGES[language].name in messages[0]["content"]
        assert "language_mismatch" in messages[0]["content"]
        assert json.loads(messages[1]["content"])["brief"]["language"] == language
    assert result["outcome"] == ("succeeded" if language == "en" else "needs_review")
    if language != "en":
        assert any(issue["code"] == "language_review_required" for issue in result["issues"])


@pytest.mark.parametrize("words", [200, 350, 550])
async def test_short_technical_profile_reaches_every_model_stage(words):
    request, gateway = scripted()
    request = CreateRunRequest(**{**request.model_dump(), "target_words": words,
                                 "editorial_profile": "technical-v1", "angle": "A focused story",
                                 "author_notes": "An author-provided observation."})
    gateway._responses["plan"]["target_words"] = words
    for task in gateway._responses["plan"]["tasks"]:
        task["target_words"] = words // 2
        for revision in range(3):
            gateway._responses[f"section:{task['id']}:{revision}"]["content_markdown"] = "dragon " * (words // 2)
    result = await build_graph(GraphDependencies(model=gateway, request=request)).compile().ainvoke(initial_state())
    assert result["outcome"] == "succeeded"
    assert result["model_calls"] == 4
    for messages in gateway.messages.values():
        assert "Never invent first-person experience" in messages[0]["content"]
        assert "unexecuted code" in messages[0]["content"]
        brief = json.loads(messages[1]["content"])["brief"]
        assert brief["angle"] == request.angle and brief["author_notes"] == request.author_notes


async def test_review_rejection_stops_at_round_cap() -> None:
    request, gateway = scripted(rejected_rounds=99)
    result = await build_graph(GraphDependencies(model=gateway, request=request)).compile().ainvoke(initial_state())
    assert result["outcome"] == "needs_review"
    assert result["revision"] == 2
    assert "review:3" not in gateway.calls
    assert result["review"]["approved"] is False


async def test_official_fixture_helper_always_discloses_synthetic_run() -> None:
    request = CreateRunRequest(topic="How forests grow", target_words=500)
    result = await build_graph(make_fixture_dependencies(request)).compile().ainvoke(initial_state(), {"recursion_limit": 50})
    assert result["outcome"] == "needs_review"
    assert result["final_markdown"].startswith("> Synthetic development draft")
    assert len(result["sources"]) == 1
    assert "## References" in result["final_markdown"]
    assert any(issue["code"] == "synthetic_fixture" for issue in result["issues"])


async def test_fixture_helper_bounds_a_long_valid_topic_title() -> None:
    request = CreateRunRequest(
        topic=("long topic " * 44).strip(), target_words=500, research_mode="required"
    )
    assert len(request.topic) > 240
    result = await build_graph(make_fixture_dependencies(request)).compile().ainvoke(
        initial_state(), {"recursion_limit": 50}
    )
    assert len(result["plan"]["title"]) <= 240
    assert result["plan"]["title"].endswith("...")


def test_section_reducer_is_idempotent_associative_and_conflict_rejecting() -> None:
    first = SectionDraft(task_id="one", title="One", content_markdown="First").model_dump(mode="json")
    second = SectionDraft(task_id="two", title="Two", content_markdown="Second").model_dump(mode="json")
    a, b = {"one:0": first}, {"two:0": second}
    assert merge_sections(a, a) == a
    assert merge_sections(merge_sections({}, a), b) == merge_sections({}, merge_sections(a, b))
    assert merge_sections(a, b) == merge_sections(b, a)
    with pytest.raises(GraphError, match="conflict"):
        merge_sections(a, {"one:0": {**first, "content_markdown": "Different"}})
    with pytest.raises(GraphError, match="identity_mismatch"):
        merge_sections({}, {"wrong:0": first})


async def test_invalid_plan_stops_before_any_worker_call() -> None:
    request, gateway = scripted()
    gateway._responses["plan"]["tasks"] = []
    with pytest.raises(ValidationError):
        await build_graph(GraphDependencies(model=gateway, request=request)).compile().ainvoke(initial_state())
    assert not gateway.finished


async def test_global_local_call_cap_stops_parallel_dispatch() -> None:
    request, gateway = scripted()
    with pytest.raises(GraphError, match="budget"):
        await build_graph(GraphDependencies(model=gateway, request=request, max_model_calls=1)).compile().ainvoke(initial_state())
    assert gateway.calls == ["plan"]


async def test_checkpoint_uses_primitives_and_resumes_failed_node() -> None:
    request, gateway = scripted()
    failed = False
    on_section_events: list[str] = []

    async def on_section(section: dict[str, Any]) -> None:
        nonlocal failed
        if section["task_id"] == "section_2" and not failed:
            failed = True
            raise RuntimeError("injected local persistence failure")
        on_section_events.append(section["task_id"])

    hooks = GraphHooks(on_section=on_section)
    memory = InMemorySaver()
    graph = build_graph(GraphDependencies(model=gateway, request=request, hooks=hooks)).compile(checkpointer=memory)
    state = initial_state()
    config = {"configurable": {"thread_id": state["run_id"]}}
    with pytest.raises(RuntimeError, match="injected"):
        await graph.ainvoke(state, config)
    result = await graph.ainvoke(None, config)
    assert len(result["sections"]) == 2
    assert result["outcome"] == "succeeded"
    assert gateway.calls.count("plan") == 1
    assert on_section_events.count("section_1") == 1
    assert on_section_events.count("section_2") == 1


async def test_explicit_worker_barrier_can_cancel_before_first_call() -> None:
    request, gateway = scripted()

    async def cancelled() -> None:
        raise RuntimeError("cancel_requested")

    graph = build_graph(GraphDependencies(model=gateway, request=request, hooks=GraphHooks(check_active=cancelled))).compile()
    with pytest.raises(RuntimeError, match="cancel_requested"):
        await graph.ainvoke(initial_state())
    assert not gateway.calls
