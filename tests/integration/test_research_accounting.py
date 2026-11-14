"""Research quotas, shared money and actual Treg transport/worker accounting."""

import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from tests.integration.test_budgets import budget_database as budget_database
from tests.integration.test_budgets import claim_run

from fieldnote.domain import CreateRunRequest
from fieldnote.domain.models import excerpt_identity
from fieldnote.graph import make_fixture_dependencies
from fieldnote.providers.fixture import FixtureGateway
from fieldnote.providers.tinyfish import TinyFishResearchGateway
from fieldnote.providers.treg import TregResearchGateway
from fieldnote.services.budgets import BudgetError, BudgetService
from fieldnote.services.research_accounting import AccountedResearch
from fieldnote.settings import Settings
from fieldnote.storage import Repository
from fieldnote.storage.pool import database_pool
from fieldnote.worker.runner import ProviderRuntime, process_claim

pytestmark = pytest.mark.asyncio
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest_asyncio.fixture
async def worker_repository(acceptance_database):
    async with database_pool(acceptance_database, migrations_directory=MIGRATIONS) as pool:
        yield Repository(pool, acceptance_database)


def service(repository, **changes):
    options = dict(call_kind="research", provider_scope="research/treg", provider_rpm=100,
        provider_tpm=1, max_run_calls=12, run_budget_usd=Decimal("1"), user_daily_budget_usd=Decimal("1"))
    options.update(changes)
    return BudgetService(repository, **options)


@pytest.mark.parametrize("requests,urls,next_urls", [(2, 100, 0), (100, 2, 1)])
async def test_request_and_url_quotas_are_distinct_and_do_not_consume_model_tokens(budget_database, requests, urls, next_urls):
    repository, _ = budget_database
    claim = await claim_run(repository)
    budget = service(repository, provider_daily_requests=requests, provider_daily_urls=urls)
    try:
        for name, url_count in [("search", 0), ("batch", 2)]:
            reserved = await budget.reserve_call(claim, name, max_tokens=0, url_count=url_count, estimated_cost_usd=Decimal(0))
            await budget.reconcile(reserved, usage_tokens=0, actual_cost_usd=Decimal(0), outcome="succeeded")
        replay = await budget.reserve_call(claim, "batch", max_tokens=0, url_count=2, estimated_cost_usd=Decimal(0))
        assert not replay.dispatch_allowed
        with pytest.raises(BudgetError, match="research_daily_quota_exhausted"):
            await budget.reserve_call(claim, "denied", max_tokens=0, url_count=next_urls, estimated_cost_usd=Decimal(0))
        with pytest.raises(BudgetError, match="call_identity_conflict"):
            await budget.reserve_call(claim, "batch", max_tokens=0, url_count=1, estimated_cost_usd=Decimal(0))
        run = await repository.get_run("budget-user", claim.run_id)
        assert run["model_call_count"] == 0 and run["research_call_count"] == 2
        async with repository.pool.connection() as connection:
            rows = await (await connection.execute("SELECT reserved_tokens,actual_tokens,reserved_urls FROM provider_calls")).fetchall()
        assert all(row["reserved_tokens"] == row["actual_tokens"] == 0 for row in rows)
        assert sum(row["reserved_urls"] for row in rows) == 2
    finally:
        await claim.close()


async def test_model_and_research_share_money_but_keep_separate_call_counts(budget_database):
    repository, _ = budget_database
    claim = await claim_run(repository)
    research = service(repository)
    model = BudgetService(repository, run_budget_usd=Decimal(1), user_daily_budget_usd=Decimal(1))
    try:
        await model.reserve_call(claim, "model", max_tokens=10, estimated_cost_usd=Decimal("0.4"))
        pending = await research.reserve_call(claim, "uncertain", max_tokens=0, estimated_cost_usd=Decimal("0.4"))
        await research.reconcile(pending, outcome="unknown")
        with pytest.raises(BudgetError, match="run_budget_exhausted"):
            await model.reserve_call(claim, "model-too-much", max_tokens=10, estimated_cost_usd=Decimal("0.3"))
        with pytest.raises(BudgetError, match="research_billing_unresolved"):
            await research.reserve_call(claim, "held", max_tokens=0, estimated_cost_usd=Decimal(0))
        await research.reconcile(pending, usage_tokens=0, actual_cost_usd=Decimal("0.2"), outcome="failed")
        await research.reserve_call(claim, "next", max_tokens=0, estimated_cost_usd=Decimal("0.3"))
        other = await claim_run(repository)
        try:
            with pytest.raises(BudgetError, match="user_daily_budget_exhausted"):
                await research.reserve_call(other, "other-run", max_tokens=0, estimated_cost_usd=Decimal("0.2"))
        finally:
            await other.close()
        run = await repository.get_run("budget-user", claim.run_id)
        assert run["model_call_count"] == 1 and run["research_call_count"] == 2
    finally:
        await claim.close()


@pytest.mark.parametrize("unknown", [False, True])
async def test_orphan_or_unknown_bill_survives_utc_rollover_and_can_reconcile_after_lease_loss(budget_database, unknown):
    repository, _ = budget_database
    first = await claim_run(repository, "first")
    second = await claim_run(repository, "second")
    budget = service(repository)
    try:
        pending = await budget.reserve_call(first, "in-flight", max_tokens=0, estimated_cost_usd=Decimal("0.4"))
        if unknown:
            await budget.reconcile(pending, outcome="unknown")
        async with repository.pool.connection() as connection:
            await connection.execute("UPDATE provider_calls SET created_at=clock_timestamp()-interval '2 days'")
            await connection.execute("UPDATE budget_reservations SET created_at=clock_timestamp()-interval '2 days'")
            await connection.execute("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s", (first.run_id,))
            await connection.commit()
        with pytest.raises(BudgetError, match="research_billing_unresolved"):
            await budget.reserve_call(second, "held-next-day", max_tokens=0, estimated_cost_usd=Decimal(0))
        await budget.reconcile(pending, usage_tokens=0, actual_cost_usd=Decimal("0.1"), outcome="failed")
        permitted = await budget.reserve_call(second, "after-receipt", max_tokens=0, estimated_cost_usd=Decimal(0))
        assert permitted.dispatch_allowed
    finally:
        await first.close()
        await second.close()


async def test_two_processes_share_research_rpm(budget_database):
    _, url = budget_database
    child = r'''
import asyncio,json,os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from fieldnote.storage import Repository
from fieldnote.storage.pool import database_pool
from fieldnote.services.budgets import BudgetService,BudgetError
async def main():
 async with database_pool(os.environ['BUDGET_TEST_URL'],migrations_directory=Path('migrations')) as pool:
  repo=Repository(pool,os.environ['BUDGET_TEST_URL'])
  await repo.create_run(uuid4().hex,{'topic':'Creative story'},uuid4().hex)
  claim=await repo.claim_job(uuid4().hex)
  try:
   service=BudgetService(repo,call_kind='research',provider_scope='research/process',provider_rpm=1)
   try:
    await service.reserve_call(claim,'search',max_tokens=0,estimated_cost_usd=Decimal(0))
    print(json.dumps({'accepted':True}))
   except BudgetError as error: print(json.dumps({'accepted':False,'code':error.code}))
  finally: await claim.close()
asyncio.run(main(),loop_factory=asyncio.SelectorEventLoop)
'''
    environment = dict(os.environ, BUDGET_TEST_URL=url, PYTHONPATH="src")

    async def invoke():
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", child], env=environment,
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    results = await asyncio.gather(invoke(), invoke())
    assert sum(row["accepted"] for row in results) == 1
    assert next(row for row in results if not row["accepted"])["code"] == "provider_quota_wait"


async def test_retry_after_blocks_other_runs_even_when_in_flight_request_succeeds(budget_database):
    repository, _ = budget_database
    first = await claim_run(repository, "first")
    second = await claim_run(repository, "second")
    budget = service(repository)
    try:
        limited = await budget.reserve_call(first, "limited", max_tokens=0, estimated_cost_usd=Decimal(0))
        in_flight = await budget.reserve_call(second, "in-flight", max_tokens=0, estimated_cost_usd=Decimal(0))
        await budget.reconcile(limited, usage_tokens=0, actual_cost_usd=Decimal(0), outcome="failed",
            retryable=True, retry_after=45)
        await budget.reconcile(in_flight, usage_tokens=0, actual_cost_usd=Decimal(0), outcome="succeeded")
        with pytest.raises(BudgetError, match="provider_circuit_open") as caught:
            await budget.reserve_call(second, "new", max_tokens=0, estimated_cost_usd=Decimal(0))
        assert caught.value.retryable and 40 <= caught.value.retry_after <= 45
    finally:
        await first.close()
        await second.close()


def treg_settings(**overrides):
    return Settings(_env_file=None, enable_external_calls=True, research_provider="treg",
        treg_qualified=True, treg_token="test-token", **overrides)


async def test_actual_worker_accounts_each_treg_request_and_keeps_model_counts_separate(worker_repository):
    repository = worker_repository
    settings = treg_settings(treg_routing_policy="budgeted", treg_max_cost_micro=10,
        run_budget_usd=Decimal("0.000050"), user_daily_budget_usd=Decimal("0.000100"))
    request = CreateRunRequest(topic="A technical explanation", target_words=350)
    base = make_fixture_dependencies(request)
    original_url, page = next(iter(base.research._pages.items()))
    original = await base.research.extract(original_url)
    new_id = excerpt_identity(original["source"]["id"], original["excerpt"]["content_hash"],
        f"characters 0:{len(page['text'])} of fetched Markdown")
    model = FixtureGateway(json.loads(json.dumps(base.model._responses).replace(original["excerpt"]["id"], new_id)))
    calls = []

    async def transport(http_request):
        calls.append(http_request.method)
        data = {"results": [{"url": original_url, "title": page["title"], "snippet": page["text"]}]} if http_request.method == "GET" else {"results": [{"text": page["text"], "title": page["title"]}]}
        return httpx.Response(200, json=data, headers={"x-treg-cost-micro": "5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        research = TregResearchGateway(client, "test-token", qualify_dns=False, routing_policy="budgeted", max_cost_micro=10)
        run = await repository.create_run(settings.local_owner_id, request.model_dump(mode="json"), uuid4().hex)
        claim = await repository.claim_job("accounted-worker")
        await process_claim(repository, claim, settings, ProviderRuntime(model=model, research=research))
    saved = await repository.get_run(settings.local_owner_id, run["id"])
    assert saved["state"] == "needs_review"
    assert saved["research_call_count"] == len(calls) > 0 and saved["model_call_count"] == len(model.calls) > 0
    async with repository.pool.connection() as connection:
        rows = await (await connection.execute("SELECT p.*,b.actual_usd,b.amount_usd FROM provider_calls p JOIN budget_reservations b ON b.run_id=p.run_id AND b.call_id=p.call_id WHERE p.call_kind='research'")).fetchall()
    assert len(rows) == len(calls) and sum(row["reserved_urls"] for row in rows) == calls.count("POST")
    assert all(row["actual_usd"] == Decimal("0.000005") and row["amount_usd"] == Decimal("0.000010") for row in rows)
    assert all(row["reserved_tokens"] == row["actual_tokens"] == 0 for row in rows)


@pytest.mark.parametrize("second_succeeds", [False, True])
async def test_actual_worker_reserves_one_request_and_each_url_for_tinyfish_batch(worker_repository, second_succeeds):
    repository = worker_repository
    settings = Settings(_env_file=None, enable_external_calls=True, research_provider="tinyfish",
                        tinyfish_api_key="test-key", tinyfish_free_qualified=True)
    request = CreateRunRequest(topic="Website availability", target_words=350)
    base = make_fixture_dependencies(request)
    original_url, page = next(iter(base.research._pages.items()))
    original = await base.research.extract(original_url)
    new_id = excerpt_identity(original["source"]["id"], original["excerpt"]["content_hash"],
                              f"characters 0:{len(page['text'])} of fetched Markdown")
    model = FixtureGateway(json.loads(json.dumps(base.model._responses).replace(original["excerpt"]["id"], new_id)))
    second = "https://example.org/another-reference"
    long_body = 'Another source with "quoted text" and Türkçe.\n' * 400
    batches = []

    def transport(http_request):
        if http_request.method == "GET":
            return httpx.Response(200, json={"results": [
                {"url": original_url, "title": page["title"]}, {"url": second, "title": "Second reference"},
            ]})
        batches.append(json.loads(http_request.content)["urls"])
        return httpx.Response(200, json={"results": [
            {"url": second, "text": long_body} if second_succeeds else {"url": second, "error": "not reachable"},
            {"url": original_url, "text": page["text"], "title": page["title"]},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        gateway = TinyFishResearchGateway(client, "test-key", qualify_dns=False)
        run = await repository.create_run(settings.local_owner_id, request.model_dump(mode="json"), uuid4().hex)
        claim = await repository.claim_job("tinyfish-accounted-worker")
        await process_claim(repository, claim, settings, ProviderRuntime(model=model, research=gateway))

    assert len(batches) == 1 and set(batches[0]) == {original_url, second}
    saved = await repository.get_run(settings.local_owner_id, run["id"])
    assert saved["state"] == "needs_review" and saved["research_call_count"] == 2
    async with repository.pool.connection() as connection:
        rows = await (await connection.execute(
            "SELECT reserved_urls, reserved_tokens, actual_tokens FROM provider_calls "
            "WHERE run_id=%s AND call_kind='research' ORDER BY reserved_urls", (run["id"],),
        )).fetchall()
    assert [row["reserved_urls"] for row in rows] == [0, 2]
    assert all(row["reserved_tokens"] == row["actual_tokens"] == 0 for row in rows)
    if second_succeeds:
        async with repository.pool.connection() as connection:
            excerpts = await (await connection.execute(
                "SELECT excerpt_data->>'text' AS text FROM evidence_excerpts WHERE run_id=%s", (run["id"],),
            )).fetchall()
        assert any(row["text"] == long_body for row in excerpts)


async def test_tinyfish_snapshots_avoid_dispatch_and_revalidate_only_for_same_owner(worker_repository):
    repository = worker_repository
    settings = Settings(_env_file=None, enable_external_calls=True, research_provider="tinyfish",
                        tinyfish_api_key="test-key", tinyfish_free_qualified=True, tinyfish_cache_seconds=900)
    url = "https://example.org/article"
    calls = []

    def transport(http_request):
        request = json.loads(http_request.content)
        calls.append(request)
        response = ({"url": url, "not_modified": True} if request.get("if_none_match") == '"v1"'
                    else {"url": url, "text": "Technical evidence.", "etag": '"v1"'})
        return httpx.Response(200, json={"results": [response], "errors": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        gateway = TinyFishResearchGateway(client, "test-key", qualify_dns=False)
        budget = service(repository, provider_scope="research/tinyfish")

        async def collect(owner, cache_seconds, *, keep_snapshot=False):
            run = await repository.create_run(owner, {"topic": "Technical topic"}, uuid4().hex)
            claim = await repository.claim_job("snapshot-worker")
            try:
                research = AccountedResearch(gateway, budget, claim, settings, cache_seconds=cache_seconds)
                snapshot, = await research.extract_many((url,))
                if keep_snapshot:
                    await repository.save_sources(claim, [snapshot["source"]], [snapshot["excerpt"]])
                async with repository.pool.connection() as connection:
                    await connection.execute("UPDATE runs SET state='needs_review' WHERE id=%s", (run["id"],))
                    await connection.commit()
                result = await repository.get_run(owner, run["id"])
                return snapshot, result["research_call_count"]
            finally:
                await claim.close()

        initial, count = await collect("cache-owner", 900, keep_snapshot=True)
        assert count == 1
        reused, count = await collect("cache-owner", 900)
        assert count == 0 and reused == initial
        other, count = await collect("other-owner", 900)
        assert count == 1 and other["source"]["retrieved_at"] != initial["source"]["retrieved_at"]
        bypassed, count = await collect("cache-owner", 0)
        assert count == 1 and "revalidated_at" not in bypassed["source"]
        async with repository.pool.connection() as connection:
            await connection.execute("SELECT pg_sleep(1.1)")
        validated, count = await collect("cache-owner", 1)
        assert count == 1 and "revalidated_at" in validated["source"]
        assert validated["excerpt"] == initial["excerpt"]
        assert validated["source"]["retrieved_at"] == initial["source"]["retrieved_at"]
        assert [item.get("if_none_match") for item in calls] == [None, None, None, '"v1"']


@pytest.mark.parametrize(("charge", "retry_after", "max_calls", "state"), [
    (None, None, 12, "failed"), (1, None, 12, "failed"),
    (0, 17, 12, "retry_wait"), (0, 17, 1, "failed"), (0, 900, 12, "failed"),
])
async def test_worker_reconciles_cost_and_respects_retry_limits(worker_repository, charge, retry_after, max_calls, state):
    repository = worker_repository
    settings = treg_settings(max_research_calls=max_calls)
    calls = []

    async def transport(request):
        calls.append(request.method)
        headers = {} if charge is None else {"x-treg-cost-micro": str(charge)}
        if retry_after is not None:
            headers["retry-after"] = str(retry_after)
            return httpx.Response(429, headers=headers)
        return httpx.Response(200, json={"results": []}, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        research = TregResearchGateway(client, "test-token", qualify_dns=False)
        run = await repository.create_run(settings.local_owner_id, CreateRunRequest(topic="Technical sources").model_dump(mode="json"), uuid4().hex)
        claim = await repository.claim_job("billing-worker")
        await process_claim(repository, claim, settings, ProviderRuntime(research=research))
    saved = await repository.get_run(settings.local_owner_id, run["id"])
    assert calls == ["GET"] and saved["model_call_count"] == 0 and saved["research_call_count"] == 1, saved["error_code"]
    assert saved["state"] == state
    if charge is None:
        assert saved["error_code"] == "research_billing_unknown"
    elif charge > 0:
        assert saved["error_code"] == "research_cost_policy_violation"
    async with repository.pool.connection() as connection:
        row = await (await connection.execute("SELECT b.* FROM budget_reservations b JOIN provider_calls p ON p.run_id=b.run_id AND p.call_id=b.call_id WHERE p.call_kind='research'")).fetchone()
        if state == "retry_wait":
            job = await (await connection.execute("SELECT due_at>clock_timestamp()+interval '15 seconds' AS delayed FROM jobs WHERE run_id=%s", (run["id"],))).fetchone()
            assert job["delayed"]
    assert row["actual_usd"] == (None if charge is None else Decimal(charge) / 1_000_000)
    assert row["status"] == ("unknown" if charge is None else "reconciled")


async def test_worker_refuses_unpriced_research_before_dispatch(worker_repository):
    repository = worker_repository
    settings = Settings(_env_file=None, enable_external_calls=True, research_provider="tavily",
        tavily_qualified=True, tavily_api_key="test-token")
    request = CreateRunRequest(topic="Technical sources")
    base = make_fixture_dependencies(request)
    run = await repository.create_run(settings.local_owner_id, request.model_dump(mode="json"), uuid4().hex)
    claim = await repository.claim_job("unpriced-worker")
    await process_claim(repository, claim, settings, ProviderRuntime(research=base.research))
    saved = await repository.get_run(settings.local_owner_id, run["id"])
    assert saved["state"] == "failed" and saved["error_code"] == "research_price_unknown"
    assert saved["research_call_count"] == saved["model_call_count"] == 0
