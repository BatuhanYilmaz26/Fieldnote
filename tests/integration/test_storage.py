"""Real PostgreSQL checks, isolated in a fresh schema per test.

Set TEST_DATABASE_URL to an explicitly disposable test database. An unavailable
database is a failed configured test, not an automatic skip. Without that env
variable these tests are visibly skipped; they never substitute SQLite/mocks.
"""

import asyncio
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import PoolTimeout

from fieldnote.storage import Repository, StorageError
from fieldnote.storage.migrate import MIGRATION_LOCK, grant_runtime, migrate, migration_files
from fieldnote.storage.pool import database_pool

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest_asyncio.fixture
async def database():
    base_url = os.environ.get("TEST_DATABASE_URL")
    if not base_url:
        pytest.skip(
            "TEST_DATABASE_URL is not configured; real PostgreSQL acceptance remains unverified"
        )
    schema = "aw_test_" + uuid4().hex
    admin = await psycopg.AsyncConnection.connect(base_url, autocommit=True)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(base_url, options=f"-c search_path={schema},public")
    try:
        applied = await migrate(scoped_url, MIGRATIONS, setup_saver=False)
        assert applied == [path.name for path in migration_files(MIGRATIONS)]
        async with database_pool(scoped_url, max_size=5, migrations_directory=MIGRATIONS) as pool:
            yield Repository(pool, scoped_url), scoped_url
    finally:
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await admin.close()


@pytest.mark.asyncio
async def test_migrations_are_repeatable_and_checksums_enforced(database, tmp_path):
    _, url = database
    assert await migrate(url, MIGRATIONS, setup_saver=False) == []
    for original in migration_files(MIGRATIONS):
        (tmp_path / original.name).write_bytes(original.read_bytes())
    candidate = tmp_path / "0001_initial.sql"
    candidate.write_text(candidate.read_text() + "\n-- changed\n")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        await migrate(url, tmp_path, setup_saver=False)


@pytest.mark.asyncio
async def test_waiting_migration_leaves_no_transaction_snapshot(database):
    _, url = database
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        waiting = asyncio.create_task(migrate(url, MIGRATIONS, setup_saver=False))
        try:
            for _ in range(20):
                row = await (await connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
                    "AND query LIKE 'SELECT pg_try_advisory_lock(%' AND state='idle' "
                    "AND datname=current_database() AND backend_xmin IS NULL)"
                )).fetchone()
                if row and row[0]:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("No waiting migration released its transaction snapshot")
            assert not waiting.done()
        finally:
            await connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
            assert await asyncio.wait_for(waiting, timeout=5) == []


@pytest.mark.asyncio
async def test_atomic_enqueue_idempotency_and_all_nonterminal_admission(database):
    repository, _ = database
    request = {"topic": "A creative story", "target_words": 500}
    same = await asyncio.gather(
        *(repository.create_run("alice", request, "same-key") for _ in range(5))
    )
    assert len({run["id"] for run in same}) == 1
    run_id = same[0]["id"]
    assert [event["sequence"] for event in await repository.list_events("alice", run_id)] == [1]
    with pytest.raises(StorageError) as conflict:
        await repository.create_run("alice", {"topic": "Changed"}, "same-key")
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(StorageError) as hidden:
        await repository.get_run("bob", run_id)
    assert hidden.value.status_code == 404
    await repository.create_run("alice", request, "second-key")
    with pytest.raises(StorageError) as full:
        await repository.create_run("alice", request, "third-key")
    assert full.value.status_code == 429
    assert len(await repository.list_runs("alice")) == 2


@pytest.mark.asyncio
async def test_idempotency_tombstone_expires_without_regenerating_inside_ttl(database):
    repository, _ = database
    request = {"topic": "A creative story"}
    run = await repository.create_run("alice", request, "expiring-key")
    await repository.delete_run("alice", run["id"])
    with pytest.raises(StorageError) as retained:
        await repository.create_run("alice", request, "expiring-key")
    assert retained.value.status_code == 410
    async with repository.pool.connection() as connection:
        await connection.execute(
            "UPDATE idempotency_keys SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE owner_id='alice' AND key='expiring-key'"
        )
    replacement = await repository.create_run("alice", request, "expiring-key")
    assert replacement["id"] != run["id"]
    assert replacement["state"] == "queued"


@pytest.mark.asyncio
async def test_two_repository_pools_never_double_claim(database):
    repository, url = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "two-pool-key")
    async with database_pool(url, migrations_directory=MIGRATIONS) as second_pool:
        second = Repository(second_pool, url)
        claims = await asyncio.gather(
            repository.claim_job("worker-one"), second.claim_job("worker-two")
        )
        acquired = [claim for claim in claims if claim is not None]
        assert len(acquired) == 1
        claim = acquired[0]
        try:
            assert claim.run_id == run["id"]
        finally:
            await claim.close()


@pytest.mark.asyncio
async def test_graph_version_mismatch_fails_closed(database):
    repository, url = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "version-key")
    async with database_pool(url, migrations_directory=MIGRATIONS) as second_pool:
        incompatible = Repository(second_pool, url, graph_version="v2")
        assert await incompatible.claim_job("version-worker") is None
    failed = await repository.get_run("alice", run["id"])
    assert failed["state"] == "failed"
    assert failed["error_code"] == "graph_version_mismatch"


@pytest.mark.asyncio
async def test_deleted_run_cannot_be_read_or_regenerated_with_retained_key(database):
    repository, _ = database
    request = {"topic": "A creative story"}
    run = await repository.create_run("alice", request, "deletion-key")
    await repository.delete_run("alice", run["id"])
    with pytest.raises(StorageError) as hidden:
        await repository.get_run("alice", run["id"])
    assert hidden.value.status_code == 404
    with pytest.raises(StorageError) as deleted:
        await repository.create_run("alice", request, "deletion-key")
    assert deleted.value.status_code == 410
    assert await repository.list_runs("alice") == []


@pytest.mark.asyncio
async def test_claims_exclude_overlap_and_expired_writer_cannot_publish(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "lease-key")
    first = await repository.claim_job("worker-one")
    assert first is not None and first.run_id == run["id"]
    try:
        assert await repository.claim_job("worker-two") is None
        async with repository.pool.connection() as connection:
            await connection.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s",
                (run["id"],),
            )
        # Lease expiration cannot steal a session lock from a paused worker.
        assert await repository.claim_job("worker-two") is None
        with pytest.raises(StorageError) as stale:
            await repository.save_section(first, "section_1", 0, {"body": "late"})
        assert stale.value.code == "lease_lost"
    finally:
        await first.close()
    second = await repository.claim_job("worker-two")
    assert second is not None and second.generation == first.generation + 1
    try:
        await repository.save_section(second, "section_1", 0, {"body": "accepted"})
        assert len(await repository.get_sections(run["id"])) == 1
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_event_sequence_is_gap_free_under_delayed_commits_and_replay_deduplicates(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "event-key")
    first_inserted = asyncio.Event()
    release_first = asyncio.Event()
    second_committed = asyncio.Event()

    async def first_writer():
        async with repository.pool.connection() as connection:
            async with connection.transaction():
                await repository._event(connection, run["id"], "first", {"n": 1}, "first")
                first_inserted.set()
                await release_first.wait()

    async def second_writer():
        await first_inserted.wait()
        async with repository.pool.connection() as connection:
            async with connection.transaction():
                await repository._event(connection, run["id"], "second", {"n": 2}, "second")
        second_committed.set()

    first = asyncio.create_task(first_writer())
    second = asyncio.create_task(second_writer())
    try:
        await asyncio.wait_for(first_inserted.wait(), timeout=5)
        await asyncio.sleep(0.2)
        # The uncommitted first event owns the run row lock, so the second
        # writer cannot allocate a later sequence and hide it from replay.
        assert not second_committed.is_set()
        release_first.set()
        await asyncio.wait_for(first, timeout=5)
        await asyncio.wait_for(second, timeout=5)
    finally:
        release_first.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    events = await repository.list_events("alice", run["id"])
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["event_type"] for event in events] == ["queued", "first", "second"]
    async with repository.pool.connection() as connection:
        async with connection.transaction():
            duplicate = await repository._event(
                connection, run["id"], "first", {"n": 1}, "first"
            )
    assert duplicate["sequence"] == 2
    assert len(await repository.list_events("alice", run["id"])) == 3


@pytest.mark.asyncio
async def test_expired_event_cursor_requires_status_resync(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "expired-key")
    claim = await repository.claim_job("expiry-worker")
    assert claim is not None
    try:
        await repository.finalize(claim, "# Draft\n\nText.", review_status="needs_review")
    finally:
        await claim.close()
    async with repository.pool.connection() as connection:
        await connection.execute(
            "DELETE FROM run_events WHERE run_id=%s AND sequence<3", (run["id"],)
        )
    with pytest.raises(StorageError) as expired:
        await repository.list_events("alice", run["id"], after=1)
    assert expired.value.code == "expired_cursor"
    assert expired.value.status_code == 410
    assert await repository.list_events("alice", run["id"], after=3) == []


@pytest.mark.asyncio
async def test_sections_events_finalization_and_human_revision_are_consistent(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "article-key")
    claim = await repository.claim_job("worker")
    assert claim is not None
    try:
        await repository.save_section(claim, "section_1", 0, {"body": "section"})
        await repository.save_section(claim, "section_1", 0, {"body": "section"})
        with pytest.raises(StorageError, match="conflicting"):
            await repository.save_section(claim, "section_1", 0, {"body": "different"})
        article = await repository.finalize(claim, "# Draft\n\nA story.", review_status="approved")
    finally:
        await claim.close()
    assert (await repository.get_run("alice", run["id"]))["state"] == "succeeded"
    assert (await repository.get_article("alice", run["id"]))["content_hash"] == article[
        "content_hash"
    ]
    events = await repository.list_events("alice", run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len([e for e in events if e["event_type"] == "section_completed"]) == 1
    edited = await repository.edit_article(
        "alice", run["id"], article["id"], "# Edited\n\nHuman changes."
    )
    assert edited["review_status"] == "unassessed"
    assert (await repository.get_run("alice", run["id"]))["state"] == "succeeded"
    assert (await repository.get_article("alice", run["id"], article["id"]))["markdown"] == article[
        "markdown"
    ]
    with pytest.raises(StorageError) as conflict:
        await repository.edit_article("alice", run["id"], article["id"], "stale edit")
    assert conflict.value.code == "revision_conflict"
    async with repository.pool.connection() as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            await connection.execute(
                "UPDATE article_revisions SET markdown='tampered' WHERE id=%s", (article["id"],)
            )


@pytest.mark.asyncio
async def test_progress_counts_only_claims_from_the_current_revision(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "current-claims")
    claim = await repository.claim_job("worker")
    assert claim is not None
    try:
        article = await repository.finalize(
            claim, "# Draft\n\nA story.", review_status="needs_review",
            claims=[{"claim_locator": "paragraph 1", "assessment": "unsupported"}],
        )
    finally:
        await claim.close()
    assert (await repository.run_progress("alice", run["id"]))["unsupported_claims"] == 1
    edited = await repository.edit_article("alice", run["id"], article["id"], "# Edited\n\nHuman changes.")
    assert edited["review_status"] == "unassessed"
    assert (await repository.run_progress("alice", run["id"]))["unsupported_claims"] == 0
    historical = await repository.get_article("alice", run["id"], article["id"])
    assert historical["claims"][0]["assessment"] == "unsupported"


@pytest.mark.asyncio
async def test_cancel_barrier_rejects_late_section_and_finalization(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "cancel-key")
    claim = await repository.claim_job("worker")
    assert claim is not None
    try:
        assert (await repository.cancel_run("alice", run["id"]))["state"] == "cancel_requested"
        with pytest.raises(StorageError) as cancelled:
            await repository.save_section(claim, "section_1", 0, {"body": "late"})
        assert cancelled.value.code == "run_cancelled"
        with pytest.raises(StorageError):
            await repository.finalize(claim, "late article", review_status="approved")
        await repository.fail_job(claim, "cancelled", "Writing was cancelled")
    finally:
        await claim.close()
    assert (await repository.get_run("alice", run["id"]))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_future_cursors_idle_worker_health_and_waiting_deadlines(database):
    repository, _ = database
    run = await repository.create_run("alice", {"topic": "A creative story"}, "deadline-key")
    with pytest.raises(StorageError) as future:
        await repository.list_events("alice", run["id"], after=100)
    assert future.value.code == "future_cursor"
    assert not await repository.ready()
    await repository.worker_heartbeat("idle-worker")
    assert await repository.ready()
    await repository.worker_heartbeat("idle-worker", draining=True)
    assert not await repository.ready()
    async with repository.pool.connection() as connection:
        await connection.execute(
            "UPDATE runs SET state='retry_wait',deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (run["id"],),
        )
        await connection.execute(
            "UPDATE jobs SET due_at=clock_timestamp()+interval '1 hour' WHERE run_id=%s",
            (run["id"],),
        )
    assert await repository.expire_waiting() == 1
    assert (await repository.get_run("alice", run["id"]))["error_code"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_runtime_role_has_data_not_ddl_and_strict_saver_roundtrips(database):
    from typing import TypedDict

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.graph import END, START, StateGraph
    from psycopg.rows import dict_row

    repository, url = database
    await migrate(url, MIGRATIONS, setup_saver=True)
    role = "aw_runtime_" + uuid4().hex
    password = secrets.token_urlsafe(32)
    admin = await psycopg.AsyncConnection.connect(url, autocommit=True)
    await admin.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
        ).format(sql.Identifier(role), sql.Literal(password))
    )
    try:
        await grant_runtime(url, role)
        runtime_url = make_conninfo(url, user=role, password=password)
        connection = await psycopg.AsyncConnection.connect(
            runtime_url, autocommit=True, row_factory=dict_row
        )
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("CREATE TABLE forbidden_ddl(id integer)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute("UPDATE schema_migrations SET checksum='tampered'")
            await connection.execute("INSERT INTO users(id) VALUES ('restricted-data-access')")

            class State(TypedDict):
                count: int

            async def increment(state):
                return {"count": state["count"] + 1}

            builder = StateGraph(State)
            builder.add_node("increment", increment)
            builder.add_edge(START, "increment")
            builder.add_edge("increment", END)
            saver = AsyncPostgresSaver(
                connection,
                serde=JsonPlusSerializer(
                    pickle_fallback=False, allowed_msgpack_modules=None, allowed_json_modules=None
                ),
            )
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "restricted-roundtrip"}}
            assert (await graph.ainvoke({"count": 1}, config, durability="sync"))["count"] == 2
        finally:
            await connection.close()
        connection = await psycopg.AsyncConnection.connect(
            runtime_url, autocommit=True, row_factory=dict_row
        )
        try:
            restored = builder.compile(
                checkpointer=AsyncPostgresSaver(
                    connection,
                    serde=JsonPlusSerializer(
                        pickle_fallback=False,
                        allowed_msgpack_modules=None,
                        allowed_json_modules=None,
                    ),
                )
            )
            assert (await restored.aget_state(config)).values["count"] == 2
        finally:
            await connection.close()
    finally:
        await admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        await admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        await admin.close()


@pytest.mark.asyncio
async def test_pool_prewarms_the_configured_minimum(database):
    _, url = database
    async with database_pool(
        url, min_size=2, max_size=3, migrations_directory=MIGRATIONS,
    ) as pool:
        stats = pool.get_stats()
        assert stats["pool_size"] == stats["pool_available"] == 2


@pytest.mark.asyncio
async def test_pool_exhaustion_is_bounded_and_schema_failure_closes_sessions(database, tmp_path):
    repository, url = database
    async with database_pool(url, max_size=1, timeout=0.2, migrations_directory=MIGRATIONS) as pool:
        async with pool.connection():
            with pytest.raises(PoolTimeout):
                async with pool.connection():
                    pytest.fail(
                        "An occupied single-connection pool unexpectedly admitted another client"
                    )

    for original in migration_files(MIGRATIONS):
        (tmp_path / original.name).write_bytes(original.read_bytes())
    candidate = tmp_path / "0001_initial.sql"
    candidate.write_text(candidate.read_text() + "\n-- checksum drift\n")
    application_name = "aw_failed_startup_" + uuid4().hex
    failed_url = make_conninfo(url, application_name=application_name)
    with pytest.raises(RuntimeError, match="incompatible"):
        async with database_pool(failed_url, migrations_directory=tmp_path):
            pytest.fail("Startup accepted a schema checksum mismatch")
    async with repository.pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT count(*) AS count FROM pg_stat_activity WHERE application_name=%s",
                (application_name,),
            )
        ).fetchone()
        assert row["count"] == 0


@pytest.mark.asyncio
async def test_retention_purges_only_old_terminal_runs_and_expired_sessions(database):
    repository, _ = database
    old = await repository.create_run("alice", {"topic": "Old article"}, "old-run")
    claim = await repository.claim_job("retention-worker")
    assert claim is not None
    await repository.finalize(claim, "# Old\n\nRetained until retention.", review_status="needs_review")
    await claim.close()
    active = await repository.create_run("alice", {"topic": "Active article"}, "active-run")
    async with repository.pool.connection() as connection:
        await connection.execute(
            "UPDATE runs SET updated_at=clock_timestamp()-interval '40 days' WHERE id=%s",
            (old["id"],),
        )
        await connection.execute(
            "INSERT INTO sessions(token_hash,owner_id,csrf_hash,expires_at) "
            "VALUES (%s,'alice',%s,clock_timestamp()-interval '1 hour')",
            ("a" * 64, "b" * 64),
        )
    removed = await repository.purge_expired(
        event_retention_days=7,
        source_retention_days=7,
        run_retention_days=30,
        deleted_grace_hours=24,
        batch_size=10,
    )
    assert removed["retained_runs"] == 1
    with pytest.raises(StorageError) as gone:
        await repository.get_run("alice", old["id"])
    assert gone.value.status_code == 404
    assert (await repository.get_run("alice", active["id"]))["state"] == "queued"
    assert await repository.prune_sessions() == 1


@pytest.mark.asyncio
async def test_retention_handles_tombstones_checkpoints_and_per_run_sources(database):
    repository, _ = database
    old = await repository.create_run("alice", {"topic": "Old article"}, "retention-old")
    claim = await repository.claim_job("retention-worker")
    assert claim is not None and claim.run_id == old["id"]
    await repository.finalize(claim, "# Old\n\nRetained until retention.", review_status="needs_review")
    await claim.close()
    bob = await repository.create_run("bob", {"topic": "Bob article"}, "retention-bob")
    bob_claim = await repository.claim_job("retention-worker")
    assert bob_claim is not None and bob_claim.run_id == bob["id"]
    await repository.finalize(bob_claim, "# Bob\n\nStill fresh.", review_status="needs_review")
    await bob_claim.close()
    recent_delete = await repository.create_run(
        "alice", {"topic": "Recent tombstone"}, "retention-recent-delete"
    )
    await repository.delete_run("alice", recent_delete["id"])
    old_delete = await repository.create_run(
        "alice", {"topic": "Old tombstone"}, "retention-old-delete"
    )
    await repository.delete_run("alice", old_delete["id"])
    active = await repository.create_run("alice", {"topic": "Active article"}, "retention-active")

    async with repository.pool.connection() as connection:
        await connection.execute(
            "UPDATE runs SET updated_at=clock_timestamp()-interval '40 days' WHERE id=%s",
            (old["id"],),
        )
        await connection.execute(
            "UPDATE runs SET deleted_at=clock_timestamp()-interval '48 hours',"
            " updated_at=clock_timestamp()-interval '48 hours' WHERE id=%s",
            (old_delete["id"],),
        )
        await connection.execute(
            "INSERT INTO sources(run_id,source_id,source_data,created_at) VALUES "
            "(%s,'alice-source','{\"title\":\"Alice\"}',clock_timestamp()-interval '10 days'),"
            "(%s,'bob-source','{\"title\":\"Bob\"}',clock_timestamp()-interval '10 days')",
            (old["id"], bob["id"]),
        )
        await connection.execute(
            "INSERT INTO run_events(run_id,sequence,event_key,event_type,payload,created_at) "
            "VALUES (%s,2,'old-active-event','progress','{}',clock_timestamp()-interval '10 days')",
            (active["id"],),
        )
        await connection.execute(
            "UPDATE idempotency_keys SET expires_at=clock_timestamp()-interval '1 hour' "
            "WHERE key IN ('retention-old','retention-old-delete')"
        )
        await connection.execute(
            "UPDATE runs SET next_event_sequence=2 WHERE id=%s", (active["id"],)
        )

    checkpoints: list[str] = []

    async def delete_checkpoint(run_id: str) -> None:
        checkpoints.append(run_id)

    removed = await repository.purge_expired(
        event_retention_days=7,
        source_retention_days=7,
        run_retention_days=30,
        deleted_grace_hours=24,
        batch_size=10,
        delete_checkpoint=delete_checkpoint,
    )
    assert removed["retained_runs"] == 1
    assert removed["deleted_runs"] == 1
    assert removed["sources"] == 1
    assert removed["events"] == 1
    assert removed["keys"] == 2
    assert set(checkpoints) == {old["id"], old_delete["id"]}

    with pytest.raises(StorageError) as old_gone:
        await repository.get_run("alice", old["id"])
    assert old_gone.value.status_code == 404
    with pytest.raises(StorageError) as old_delete_gone:
        await repository.get_run("alice", old_delete["id"])
    assert old_delete_gone.value.status_code == 404

    # The tombstone inside the grace window survives; the other owner's run and
    # its source are untouched because retention is run-scoped.
    assert (await repository.get_run("bob", bob["id"]))["state"] == "needs_review"
    assert [source["title"] for source in await repository.sources("bob", bob["id"])] == ["Bob"]
    assert (await repository.get_run("alice", active["id"]))["state"] == "queued"
    async with repository.pool.connection() as connection:
        tombstone = await (
            await connection.execute(
                "SELECT count(*) AS count FROM runs WHERE id=%s AND deleted_at IS NOT NULL",
                (recent_delete["id"],),
            )
        ).fetchone()
        old_events = await (
            await connection.execute(
                "SELECT count(*) AS count FROM run_events WHERE event_key='old-active-event'"
            )
        ).fetchone()
        keys = await (
            await connection.execute(
                "SELECT count(*) AS count FROM idempotency_keys "
                "WHERE key IN ('retention-old','retention-old-delete')"
            )
        ).fetchone()
        active_key = await (
            await connection.execute(
                "SELECT count(*) AS count FROM idempotency_keys WHERE key='retention-active'"
            )
        ).fetchone()
    assert tombstone is not None and tombstone["count"] == 1
    assert old_events is not None and old_events["count"] == 0
    assert keys is not None and keys["count"] == 0
    assert active_key is not None and active_key["count"] == 1


@pytest.mark.asyncio
async def test_database_session_loss_fences_old_saver_and_allows_successor(database):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    repository, _ = database
    await repository.create_run("alice", {"topic": "A creative story"}, "session-loss")
    first = await repository.claim_job("old-worker")
    assert first is not None
    successor = None
    try:
        async with repository.pool.connection() as connection:
            terminated = await (
                await connection.execute(
                    "SELECT pg_terminate_backend(%s) AS terminated", (first.backend_pid,)
                )
            ).fetchone()
            assert terminated["terminated"]
            await connection.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=%s",
                (first.run_id,),
            )
        with pytest.raises(StorageError, match="lost"):
            await repository.save_section(first, "section_1", 0, {"body": "stale"})
        saver = AsyncPostgresSaver(
            first.connection,
            serde=JsonPlusSerializer(
                pickle_fallback=False, allowed_msgpack_modules=None, allowed_json_modules=None
            ),
        )
        with pytest.raises(psycopg.Error):
            await saver.aget_tuple({"configurable": {"thread_id": first.run_id}})
        successor = await repository.claim_job("new-worker")
        assert successor is not None and successor.generation == first.generation + 1
        await repository.save_section(successor, "section_1", 0, {"body": "new worker"})
    finally:
        if successor is not None:
            await successor.close()
        await first.close()


def test_strict_checkpoint_deserialization_does_not_reconstruct_unknown_types():
    from dataclasses import dataclass

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    constructions = []

    @dataclass
    class UnknownCheckpointType:
        value: str

        def __post_init__(self):
            constructions.append(self.value)

    serializer = JsonPlusSerializer(
        pickle_fallback=False, allowed_msgpack_modules=None, allowed_json_modules=None
    )
    encoded = serializer.dumps_typed(UnknownCheckpointType("sentinel"))
    constructions.clear()
    decoded = serializer.loads_typed(encoded)
    assert not isinstance(decoded, UnknownCheckpointType)
    assert constructions == []
