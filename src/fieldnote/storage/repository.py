"""Transactional owner-scoped persistence and leased job ownership.

All provider work happens outside short application transactions. A Claim's
dedicated connection owns both the session advisory lock and the graph saver;
it must not reconnect or be returned to a connection pool.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

TERMINAL_STATES = frozenset({"succeeded", "needs_review", "failed", "cancelled"})
NONTERMINAL_STATES = ("queued", "running", "retry_wait", "cancel_requested")
ADMISSION_LOCK = 1195461970


class StorageError(Exception):
    """Public-safe persistence/business error, never an upstream SQL message."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def canonical_hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def markdown_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _uuid(value: str | UUID) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as error:
        raise StorageError("not_found", "Run or revision not found", 404) from error


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_public(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclass
class Claim:
    connection: psycopg.AsyncConnection[dict[str, Any]]
    run: dict[str, Any]
    worker_id: str
    generation: int
    lease_seconds: int
    advisory_key: int
    backend_pid: int

    @property
    def run_id(self) -> str:
        return str(self.run["id"])

    async def close(self) -> None:
        """Call only after graph tasks stop; closing releases the run lock."""
        await self.connection.close()


class Repository:
    def __init__(
        self,
        pool: AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]],
        database_url: str,
        *,
        max_user_runs: int = 2,
        max_global_runs: int = 100,
        max_run_seconds: int = 600,
        graph_version: str = "v1",
    ):
        self.pool = pool
        self.database_url = database_url
        self.max_user_runs = max_user_runs
        self.max_global_runs = max_global_runs
        self.max_run_seconds = max_run_seconds
        self.graph_version = graph_version

    async def _owned(
        self, connection: Any, owner: str, run_id: str | UUID, *, lock: bool = False
    ) -> dict:
        query = "SELECT * FROM runs WHERE id=%s AND owner_id=%s AND deleted_at IS NULL"
        if lock:
            query += " FOR UPDATE"
        row = await (await connection.execute(query, (_uuid(run_id), owner))).fetchone()
        if row is None:
            raise StorageError("not_found", "Run not found", 404)
        return row

    async def topic_snapshot(self, owner: str, *, refresh: bool = False) -> tuple[dict | None, bool]:
        """One refresh per owner per five minutes, serialized across replicas."""
        async with self.pool.connection() as connection, connection.transaction():
            if refresh:
                row = await (await connection.execute(
                    "INSERT INTO topic_discovery(owner_id) VALUES (%s) ON CONFLICT(owner_id) "
                    "DO UPDATE SET started_at=clock_timestamp() "
                    "WHERE topic_discovery.started_at<clock_timestamp()-interval '5 minutes' RETURNING owner_id",
                    (owner,),
                )).fetchone()
                if row:
                    return None, True
            row = await (await connection.execute(
                "SELECT snapshot FROM topic_discovery WHERE owner_id=%s", (owner,),
            )).fetchone()
            return row["snapshot"] if row else None, False

    async def save_topic_snapshot(self, owner: str, snapshot: dict) -> None:
        async with self.pool.connection() as connection:
            await connection.execute("UPDATE topic_discovery SET snapshot=%s WHERE owner_id=%s",
                                     (Jsonb(snapshot), owner))

    async def previous_briefs(self, owner: str) -> list[dict]:
        async with self.pool.connection() as connection:
            rows = await (await connection.execute(
                "SELECT request FROM runs WHERE owner_id=%s AND kind='generation' "
                "AND deleted_at IS NULL ORDER BY accepted_at DESC,id DESC LIMIT 200", (owner,),
            )).fetchall()
            return [row["request"] for row in rows]

    async def save_publication_package(
        self, owner: str, run_id: str, revision_id: str, content_hash: str,
        inputs: dict, data: dict,
    ) -> dict:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if run["kind"] != "generation" or run["state"] not in TERMINAL_STATES or run["current_revision"] != _uuid(revision_id):
                raise StorageError("revision_conflict", "Save and select the latest article revision before preparing publication.")
            article = await (await connection.execute(
                "SELECT content_hash FROM article_revisions WHERE id=%s AND run_id=%s",
                (_uuid(revision_id), _uuid(run_id)),
            )).fetchone()
            if not article or article["content_hash"] != content_hash:
                raise StorageError("revision_conflict", "The saved article no longer matches this package.")
            digest = canonical_hash({"inputs": inputs, "assessment_review_id": data.get("assessment_review_id"),
                                     "article_review_status": data.get("article_review_status"),
                                     "package_schema_version": data.get("schema_version", 1)})
            existing = await (await connection.execute(
                "SELECT * FROM publication_packages WHERE revision_id=%s AND input_hash=%s",
                (_uuid(revision_id), digest),
            )).fetchone()
            if existing:
                return _public(existing)
            count = await (await connection.execute(
                "SELECT count(*) AS n FROM publication_packages WHERE run_id=%s", (_uuid(run_id),),
            )).fetchone()
            assert count is not None
            if count["n"] >= 50:
                raise StorageError("package_limit", "This draft already has 50 saved packages. Start another draft to prepare more.", 422)
            row = await (await connection.execute(
                "INSERT INTO publication_packages(id,run_id,revision_id,content_hash,input_hash,data) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (uuid4(), _uuid(run_id), _uuid(revision_id), content_hash, digest, Jsonb(data)),
            )).fetchone()
            return _public(row)

    async def publication_packages(self, owner: str, run_id: str) -> list[dict]:
        async with self.pool.connection() as connection:
            run = await self._owned(connection, owner, run_id)
            rows = await (await connection.execute(
                "SELECT id,revision_id,content_hash,created_at FROM publication_packages "
                "WHERE run_id=%s ORDER BY created_at DESC,id DESC LIMIT 50", (_uuid(run_id),),
            )).fetchall()
            return [{**_public(row), "stale": row["revision_id"] != run["current_revision"]} for row in rows]

    async def publication_package(self, owner: str, run_id: str, package_id: str) -> dict:
        async with self.pool.connection() as connection:
            run = await self._owned(connection, owner, run_id)
            row = await (await connection.execute(
                "SELECT * FROM publication_packages WHERE id=%s AND run_id=%s",
                (_uuid(package_id), _uuid(run_id)),
            )).fetchone()
            if not row:
                raise StorageError("package_not_found", "Publication package not found.", 404)
            assessment = await (await connection.execute(
                "SELECT a.review_run_id FROM revision_assessments a JOIN runs j ON j.id=a.review_run_id "
                "WHERE a.revision_id=%s AND j.deleted_at IS NULL "
                "ORDER BY a.created_at DESC,a.review_run_id DESC LIMIT 1", (row["revision_id"],),
            )).fetchone()
            review_id = str(assessment["review_run_id"]) if assessment else None
            stale = row["revision_id"] != run["current_revision"]
            review_changed = review_id != row["data"].get("assessment_review_id")
            return {**_public(row), "stale": stale or review_changed, "review_changed": review_changed}

    async def record_publication(self, owner: str, run_id: str, package_id: str, record: dict) -> dict:
        from datetime import UTC, timedelta

        if datetime.fromisoformat(record["published_at"]) > datetime.now(UTC) + timedelta(minutes=5):
            raise StorageError("invalid_publication_date", "Record a past publication date, not a future schedule.", 422)
        async with self.pool.connection() as connection, connection.transaction():
            await self._owned(connection, owner, run_id, lock=True)
            package = await (await connection.execute(
                "SELECT id FROM publication_packages WHERE id=%s AND run_id=%s",
                (_uuid(package_id), _uuid(run_id)),
            )).fetchone()
            if not package:
                raise StorageError("package_not_found", "Publication package not found.", 404)
            existing = await (await connection.execute(
                "SELECT * FROM publications WHERE package_id=%s AND platform=%s AND url=%s",
                (_uuid(package_id), record["platform"], record["url"]),
            )).fetchone()
            if existing:
                return _public(existing)
            count = await (await connection.execute(
                "SELECT count(*) AS n FROM publications WHERE package_id=%s", (_uuid(package_id),),
            )).fetchone()
            assert count is not None
            if count["n"] >= 15:
                raise StorageError("publication_limit", "This package already has 15 publication records.", 422)
            row = await (await connection.execute(
                "INSERT INTO publications(id,package_id,platform,url,published_at) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (uuid4(), _uuid(package_id), record["platform"], record["url"], record["published_at"]),
            )).fetchone()
            return _public(row)

    async def publication_history(self, owner: str, run_id: str) -> list[dict]:
        async with self.pool.connection() as connection:
            await self._owned(connection, owner, run_id)
            rows = await (await connection.execute(
                "SELECT p.*,k.revision_id,k.content_hash FROM publications p "
                "JOIN publication_packages k ON k.id=p.package_id WHERE k.run_id=%s "
                "ORDER BY p.published_at DESC,p.id DESC LIMIT 100", (_uuid(run_id),),
            )).fetchall()
            return _public(rows)

    async def _event(
        self, connection: Any, run_id: str | UUID, event_type: str, payload: dict, event_key: str
    ) -> dict:
        # Lock first: duplicate identity checks and sequence allocation have the
        # same serialization order as the state transaction that publishes them.
        await connection.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (_uuid(run_id),))
        existing = await (
            await connection.execute(
                "SELECT * FROM run_events WHERE run_id=%s AND event_key=%s",
                (_uuid(run_id), event_key),
            )
        ).fetchone()
        if existing:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise StorageError("event_conflict", "An event identity has conflicting contents")
            return existing
        row = await (
            await connection.execute(
                "UPDATE runs SET next_event_sequence=next_event_sequence+1,updated_at=clock_timestamp() "
                "WHERE id=%s RETURNING next_event_sequence",
                (_uuid(run_id),),
            )
        ).fetchone()
        return await (
            await connection.execute(
                "INSERT INTO run_events(run_id,sequence,event_key,event_type,payload) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (_uuid(run_id), row["next_event_sequence"], event_key, event_type, Jsonb(payload)),
            )
        ).fetchone()

    async def create_session(
        self, owner: str, token_hash: str, csrf_hash: str, ttl_hours: int
    ) -> None:
        """Persist one opaque browser session; only hashes are stored."""
        if not owner or len(owner) > 255:
            raise StorageError("invalid_principal", "Invalid authenticated principal", 401)
        if not re.fullmatch(r"[a-f0-9]{64}", token_hash) or not re.fullmatch(
            r"[a-f0-9]{64}", csrf_hash
        ):
            raise ValueError("Session hashes must be SHA-256 hex digests")
        if not 1 <= ttl_hours <= 168:
            raise ValueError("Invalid session lifetime")
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO users(id) VALUES (%s) ON CONFLICT DO NOTHING", (owner,)
            )
            await connection.execute(
                "INSERT INTO sessions(token_hash,owner_id,csrf_hash,expires_at) "
                "VALUES (%s,%s,%s,clock_timestamp()+(%s*interval '1 hour'))",
                (token_hash, owner, csrf_hash, ttl_hours),
            )
            # Bounded opportunistic cleanup; explicit retention remains a job.
            await connection.execute(
                "DELETE FROM sessions WHERE ctid IN (SELECT ctid FROM sessions "
                "WHERE expires_at<=clock_timestamp() OR "
                "(revoked_at IS NOT NULL AND revoked_at<clock_timestamp()-interval '1 day') "
                "LIMIT 100)"
            )

    async def get_session(self, token_hash: str) -> dict | None:
        """Return a live session by token hash; never returns the raw token."""
        if not re.fullmatch(r"[a-f0-9]{64}", token_hash):
            return None
        async with self.pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT owner_id,csrf_hash,expires_at FROM sessions "
                    "WHERE token_hash=%s AND revoked_at IS NULL "
                    "AND expires_at>clock_timestamp()",
                    (token_hash,),
                )
            ).fetchone()
            return _public(row) if row else None

    async def set_session_csrf(self, token_hash: str, csrf_hash: str) -> bool:
        """Rotate the CSRF digest after a process key change or explicit refresh."""
        if not re.fullmatch(r"[a-f0-9]{64}", csrf_hash):
            raise ValueError("Invalid CSRF digest")
        async with self.pool.connection() as connection:
            result = await connection.execute(
                "UPDATE sessions SET csrf_hash=%s WHERE token_hash=%s "
                "AND revoked_at IS NULL AND expires_at>clock_timestamp()",
                (csrf_hash, token_hash),
            )
            return result.rowcount == 1

    async def revoke_session(self, token_hash: str) -> bool:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                "UPDATE sessions SET revoked_at=clock_timestamp() "
                "WHERE token_hash=%s AND revoked_at IS NULL",
                (token_hash,),
            )
            return result.rowcount == 1

    async def revoke_owner_sessions(self, owner: str) -> int:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                "UPDATE sessions SET revoked_at=clock_timestamp() "
                "WHERE owner_id=%s AND revoked_at IS NULL",
                (owner,),
            )
            return result.rowcount

    async def prune_sessions(self) -> int:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                "DELETE FROM sessions WHERE expires_at<=clock_timestamp() "
                "OR (revoked_at IS NOT NULL AND revoked_at<clock_timestamp()-interval '1 day')"
            )
            return result.rowcount

    async def create_run(
        self, owner: str, request: dict, idempotency_key: str,
        *, review_of: tuple[str, str, str] | None = None,
    ) -> dict:
        if not 1 <= len(idempotency_key) <= 200 or not idempotency_key.isascii():
            raise StorageError(
                "invalid_idempotency_key", "Use an ASCII idempotency key of 1–200 characters", 422
            )
        if not owner or len(owner) > 255:
            raise StorageError("invalid_principal", "Invalid authenticated principal", 401)
        digest = canonical_hash(request if review_of is None else {
            "kind": "revision_review", "subject": review_of, "request": request,
        })
        async with self.pool.connection() as connection, connection.transaction():
            # Admission is deliberately serialized initially: user/global caps
            # must not be defeated by concurrent submissions on several APIs.
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (ADMISSION_LOCK,))
            await connection.execute(
                "INSERT INTO users(id) VALUES (%s) ON CONFLICT DO NOTHING", (owner,)
            )
            await connection.execute(
                "DELETE FROM idempotency_keys WHERE owner_id=%s AND key=%s AND expires_at<=clock_timestamp()",
                (owner, idempotency_key),
            )
            existing = await (
                await connection.execute(
                    "SELECT i.request_hash,r.* FROM idempotency_keys i JOIN runs r ON r.id=i.run_id "
                    "WHERE i.owner_id=%s AND i.key=%s",
                    (owner, idempotency_key),
                )
            ).fetchone()
            if existing:
                if existing["deleted_at"] is not None:
                    raise StorageError(
                        "run_deleted", "This idempotency key belongs to a deleted run", 410
                    )
                if existing["request_hash"] != digest:
                    raise StorageError(
                        "idempotency_conflict", "This key was used for a different writing brief"
                    )
                return _public(existing)
            revision_id = None
            if review_of is not None:
                subject_run, subject_revision, expected_hash = review_of
                parent = await self._owned(connection, owner, subject_run, lock=True)
                if (parent["kind"] != "generation" or parent["state"] not in TERMINAL_STATES
                        or parent["current_revision"] != _uuid(subject_revision)):
                    raise StorageError("revision_conflict", "Select the latest saved revision before requesting a review.")
                revision = await (await connection.execute(
                    "SELECT * FROM article_revisions WHERE id=%s AND run_id=%s",
                    (_uuid(subject_revision), _uuid(subject_run)),
                )).fetchone()
                if (revision is None or revision["author_type"] != "human"
                        or revision["content_hash"] != expected_hash):
                    raise StorageError("revision_conflict", "The saved revision no longer matches this review request.")
                active = await (await connection.execute(
                    "SELECT id FROM runs WHERE review_revision_id=%s AND deleted_at IS NULL AND state=ANY(%s)",
                    (_uuid(subject_revision), list(NONTERMINAL_STATES)),
                )).fetchone()
                if active:
                    raise StorageError("review_pending", "A review is already queued for this saved revision.")
                revision_id = _uuid(subject_revision)
                await connection.execute(
                    "UPDATE runs SET updated_at=clock_timestamp() WHERE id=%s", (_uuid(subject_run),),
                )
            counts = await (
                await connection.execute(
                    "SELECT count(*) AS total,count(*) FILTER (WHERE owner_id=%s) AS owned "
                    "FROM runs WHERE state=ANY(%s)",
                    (owner, list(NONTERMINAL_STATES)),
                )
            ).fetchone()
            assert counts is not None  # An aggregate SELECT always returns one row.
            if counts["total"] >= self.max_global_runs or counts["owned"] >= self.max_user_runs:
                raise StorageError(
                    "queue_full", "The writing queue is full. Try again after a run finishes.", 429
                )
            run_id = uuid4()
            row = await (
                await connection.execute(
                    "INSERT INTO runs(id,owner_id,request,request_hash,deadline_at,graph_version,kind,review_revision_id) "
                    "VALUES (%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'),%s,%s,%s) RETURNING *",
                    (
                        run_id,
                        owner,
                        Jsonb(request),
                        digest,
                        self.max_run_seconds,
                        self.graph_version,
                        "revision_review" if review_of else "generation",
                        revision_id,
                    ),
                )
            ).fetchone()
            await connection.execute("INSERT INTO jobs(run_id) VALUES (%s)", (run_id,))
            await connection.execute(
                "INSERT INTO idempotency_keys(owner_id,key,request_hash,run_id,expires_at) "
                "VALUES (%s,%s,%s,%s,clock_timestamp()+interval '24 hours')",
                (owner, idempotency_key, digest, run_id),
            )
            await self._event(connection, run_id, "queued", {"state": "queued"}, "queued")
            assert row is not None
            row["next_event_sequence"] = 1
            return _public(row)

    async def create_revision_review(
        self, owner: str, run_id: str, revision_id: str, expected_hash: str, idempotency_key: str,
    ) -> dict:
        parent = await self.get_run(owner, run_id)
        return await self.create_run(
            owner, parent["request"], idempotency_key,
            review_of=(run_id, revision_id, expected_hash),
        )

    async def get_run(self, owner: str, run_id: str) -> dict:
        async with self.pool.connection() as connection:
            return _public(await self._owned(connection, owner, run_id))

    async def get_run_internal(self, run_id: str) -> dict:
        """Worker-only run lookup; callers must already own the execution fence."""
        async with self.pool.connection() as connection:
            row = await (
                await connection.execute("SELECT * FROM runs WHERE id=%s", (_uuid(run_id),))
            ).fetchone()
            if row is None:
                raise StorageError("not_found", "Run not found", 404)
            return _public(row)

    async def run_progress(self, owner: str, run_id: str) -> dict:
        """Committed progress for the authoritative status endpoint."""
        async with self.pool.connection() as connection:
            await self._owned(connection, owner, run_id)
            row = await (
                await connection.execute(
                    "SELECT (SELECT count(DISTINCT s.task_id) FROM section_drafts s "
                    " WHERE s.run_id=r.id AND s.content->>'status'='accepted') AS sections,"
                    "COALESCE((SELECT jsonb_array_length(p.plan->'tasks') FROM plans p "
                    " WHERE p.run_id=r.id ORDER BY p.revision DESC LIMIT 1),0) AS tasks,"
                    "(SELECT count(*) FROM sources s WHERE s.run_id=r.id) AS sources,"
                    "(SELECT count(*) FROM evidence_excerpts e WHERE e.run_id=r.id) AS excerpts,"
                    "(SELECT count(*) FROM claim_citations c JOIN article_revisions a ON a.id=c.revision_id "
                    " WHERE a.id=r.current_revision AND c.assessment->>'assessment' IN ('unsupported','conflicting')) AS unsupported_claims "
                    "FROM runs r WHERE r.id=%s",
                    (_uuid(run_id),),
                )
            ).fetchone()
            assert row is not None
            result = dict(row)
            assessment = await (await connection.execute(
                "SELECT claims FROM revision_assessments a JOIN runs j ON j.id=a.review_run_id "
                "WHERE a.revision_id=(SELECT current_revision FROM runs WHERE id=%s) "
                "AND j.deleted_at IS NULL ORDER BY a.created_at DESC,a.review_run_id DESC LIMIT 1",
                (_uuid(run_id),),
            )).fetchone()
            if assessment:
                result["unsupported_claims"] = sum(
                    item.get("assessment") in {"unsupported", "conflicting"}
                    for item in assessment["claims"]
                )
            return result

    async def list_runs(
        self, owner: str, *, limit: int = 20, before: str | None = None
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise StorageError("invalid_limit", "Page size must be between 1 and 100", 422)
        async with self.pool.connection() as connection:
            params: list[Any] = [owner]
            query = "SELECT * FROM runs WHERE owner_id=%s AND deleted_at IS NULL AND kind='generation'"
            if before:
                cursor = await self._owned(connection, owner, before)
                query += " AND (accepted_at,id)<(%s,%s)"
                params.extend([cursor["accepted_at"], cursor["id"]])
            query += " ORDER BY accepted_at DESC,id DESC LIMIT %s"
            params.append(limit)
            return _public(await (await connection.execute(query, params)).fetchall())

    async def list_events(
        self, owner: str, run_id: str, *, after: int = 0, limit: int = 100
    ) -> list[dict]:
        if after < 0 or not 1 <= limit <= 500:
            raise StorageError("invalid_cursor", "Invalid event cursor or page size", 422)
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if after > run["next_event_sequence"]:
                raise StorageError("future_cursor", "Event cursor is ahead of this run", 409)
            first = await (
                await connection.execute(
                    "SELECT min(sequence) AS sequence FROM run_events WHERE run_id=%s",
                    (_uuid(run_id),),
                )
            ).fetchone()
            assert first is not None
            if after and (
                (first["sequence"] is None and after < run["next_event_sequence"])
                or (first["sequence"] is not None and after < first["sequence"] - 1)
            ):
                raise StorageError(
                    "expired_cursor", "Event history expired. Reload the run status.", 410
                )
            rows = await (
                await connection.execute(
                    "SELECT * FROM run_events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                    (_uuid(run_id), after, limit),
                )
            ).fetchall()
            return _public(rows)

    async def cancel_run(self, owner: str, run_id: str) -> dict:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if run["state"] in TERMINAL_STATES or run["state"] == "cancel_requested":
                return _public(run)
            state = "cancelled" if run["state"] in {"queued", "retry_wait"} else "cancel_requested"
            updated = await (
                await connection.execute(
                    "UPDATE runs SET state=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                    (state, _uuid(run_id)),
                )
            ).fetchone()
            assert updated is not None
            run = updated
            await self._event(connection, run_id, state, {"state": state}, state)
            return _public(run)

    async def delete_run(self, owner: str, run_id: str) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            state = run["state"]
            if state in {"queued", "retry_wait"}:
                state = "cancelled"
            elif state not in TERMINAL_STATES:
                state = "cancel_requested"
            await connection.execute(
                "UPDATE runs SET state=%s,deleted_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
                (state, _uuid(run_id)),
            )
            await self._event(connection, run_id, "deleted", {"state": state}, "deleted")
            # Deleting an article also stops its otherwise hidden review jobs.
            if run["kind"] == "generation":
                reviews = await (await connection.execute(
                    "UPDATE runs SET state=CASE WHEN state IN ('queued','retry_wait') THEN 'cancelled' "
                    "WHEN state IN ('running','cancel_requested') THEN 'cancel_requested' ELSE state END,"
                    "deleted_at=clock_timestamp(),updated_at=clock_timestamp() "
                    "WHERE review_revision_id IN (SELECT id FROM article_revisions WHERE run_id=%s) "
                    "AND deleted_at IS NULL RETURNING id,state", (_uuid(run_id),),
                )).fetchall()
                for review in reviews:
                    await self._event(connection, review["id"], "deleted", {"state": review["state"]}, "deleted")

    async def sources(
        self, owner: str, run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        if not 1 <= limit <= 100 or offset < 0:
            raise StorageError("invalid_page", "Invalid source page", 422)
        async with self.pool.connection() as connection:
            await self._owned(connection, owner, run_id)
            rows = await (
                await connection.execute(
                    "SELECT source_data FROM sources WHERE run_id=%s ORDER BY source_id LIMIT %s OFFSET %s",
                    (_uuid(run_id), limit, offset),
                )
            ).fetchall()
            return [row["source_data"] for row in rows]

    async def get_article(self, owner: str, run_id: str, revision_id: str | None = None) -> dict:
        async with self.pool.connection() as connection:
            run = await self._owned(connection, owner, run_id)
            selected = _uuid(revision_id) if revision_id else run["current_revision"]
            row = await (
                await connection.execute(
                    "SELECT * FROM article_revisions WHERE run_id=%s AND id=%s",
                    (_uuid(run_id), selected),
                )
            ).fetchone()
            if row is None:
                raise StorageError("article_not_found", "No article revision is available", 404)
            claims = await (
                await connection.execute(
                    "SELECT claim_id,assessment FROM claim_citations WHERE revision_id=%s ORDER BY claim_id",
                    (selected,),
                )
            ).fetchall()
            result = _public(row)
            result["claims"] = [dict(item["assessment"], claim_id=item["claim_id"]) for item in claims]
            assessment = await (await connection.execute(
                "SELECT a.* FROM revision_assessments a JOIN runs j ON j.id=a.review_run_id "
                "WHERE a.revision_id=%s AND a.content_hash=%s AND j.deleted_at IS NULL "
                "ORDER BY a.created_at DESC,a.review_run_id DESC LIMIT 1", (selected, row["content_hash"]),
            )).fetchone()
            result["assessment"] = _public(assessment) if assessment else None
            if assessment:
                result["claims"] = _public(assessment["claims"])
            job = await (await connection.execute(
                "SELECT id,state,error_code,error_message,model_call_count,accepted_at FROM runs "
                "WHERE review_revision_id=%s AND deleted_at IS NULL "
                "ORDER BY accepted_at DESC,id DESC LIMIT 1", (selected,),
            )).fetchone()
            result["assessment_job"] = _public(job) if job else None
            return result

    async def get_cited_evidence(self, owner: str, run_id: str, revision_id: str, excerpt_id: str) -> dict:
        article = await self.get_article(owner, run_id, revision_id)
        hashes = {
            digest
            for claim in article["claims"]
            for cited_id, digest in zip(claim.get("excerpt_ids", []), claim.get("excerpt_hashes", []), strict=False)
            if cited_id == excerpt_id
        }
        if hashes:
            async with self.pool.connection() as connection:
                row = await (await connection.execute(
                    "SELECT e.excerpt_data,s.source_data FROM evidence_excerpts e "
                    "JOIN sources s ON s.run_id=e.run_id AND s.source_id=e.source_id "
                    "JOIN runs r ON r.id=e.run_id "
                    "WHERE e.run_id=%s AND e.id=%s AND r.owner_id=%s AND r.deleted_at IS NULL",
                    (_uuid(run_id), excerpt_id, owner),
                )).fetchone()
            if row:
                excerpt = row["excerpt_data"]
                if hashes == {excerpt["content_hash"]} and markdown_hash(excerpt["text"]) in hashes:
                    return {"source": row["source_data"], "excerpt": excerpt}
        raise StorageError("evidence_not_found", "Saved evidence is unavailable for this revision.", 404)

    async def list_revisions(self, owner: str, run_id: str, *, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 100:
            raise StorageError("invalid_limit", "Page size must be between 1 and 100", 422)
        async with self.pool.connection() as connection:
            await self._owned(connection, owner, run_id)
            return _public(
                await (
                    await connection.execute(
                        "SELECT a.id,a.parent_revision,a.content_hash,a.review_status,a.author_type,a.created_at,"
                        "(SELECT v.review_status FROM revision_assessments v JOIN runs j ON j.id=v.review_run_id "
                        "WHERE v.revision_id=a.id AND j.deleted_at IS NULL "
                        "ORDER BY v.created_at DESC,v.review_run_id DESC LIMIT 1) AS assessment_status "
                        "FROM article_revisions a WHERE a.run_id=%s ORDER BY a.created_at DESC,a.id DESC LIMIT %s",
                        (_uuid(run_id), limit),
                    )
                ).fetchall()
            )

    async def edit_article(
        self, owner: str, run_id: str, parent_revision: str, markdown: str,
        *, citation_map: dict | None = None,
    ) -> dict:
        if not markdown.strip() or len(markdown) > 200000:
            raise StorageError(
                "invalid_markdown", "Article text must contain 1–200,000 characters", 422
            )
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._owned(connection, owner, run_id, lock=True)
            if run["state"] not in TERMINAL_STATES or run["current_revision"] != _uuid(
                parent_revision
            ):
                raise StorageError(
                    "revision_conflict", "The article changed. Reload before saving your edit."
                )
            parent = await (
                await connection.execute(
                    "SELECT citation_map FROM article_revisions WHERE id=%s",
                    (_uuid(parent_revision),),
                )
            ).fetchone()
            assert parent is not None  # Protected by current_revision's foreign key.
            revision_id = uuid4()
            row = await (
                await connection.execute(
                    "INSERT INTO article_revisions(id,run_id,parent_revision,markdown,content_hash,review_status,author_type,citation_map) "
                    "VALUES (%s,%s,%s,%s,%s,'unassessed','human',%s) RETURNING *",
                    (
                        revision_id,
                        _uuid(run_id),
                        _uuid(parent_revision),
                        markdown,
                        markdown_hash(markdown),
                        Jsonb(parent["citation_map"] if citation_map is None else citation_map),
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE runs SET current_revision=%s WHERE id=%s", (revision_id, _uuid(run_id))
            )
            await self._event(
                connection,
                run_id,
                "article_edited",
                {"revision_id": str(revision_id), "review_status": "unassessed"},
                f"edit:{revision_id}",
            )
            return _public(row)

    async def review_subject(self, claim: Claim) -> dict:
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                "SELECT a.*,r.owner_id,r.request,r.policy,r.id AS subject_run_id "
                "FROM article_revisions a JOIN runs r ON r.id=a.run_id "
                "WHERE a.id=%s AND r.owner_id=%s AND r.deleted_at IS NULL AND r.kind='generation'",
                (claim.run.get("review_revision_id"), claim.run["owner_id"]),
            )).fetchone()
            if row is None:
                raise StorageError("review_subject_missing", "The saved revision is no longer available.", 404)
            if markdown_hash(row["markdown"]) != row["content_hash"]:
                raise StorageError("review_integrity_mismatch", "The saved revision failed its integrity check.")
            excerpts = await (await connection.execute(
                "SELECT id,excerpt_data FROM evidence_excerpts WHERE run_id=%s ORDER BY id LIMIT 100",
                (row["subject_run_id"],),
            )).fetchall()
            return {"article": _public(row), "excerpts": {
                item["id"]: item["excerpt_data"] for item in excerpts
            }}

    async def finalize_review(self, claim: Claim, review: dict, claims: list[dict]) -> dict:
        subject = await self.review_subject(claim)
        article = subject["article"]
        if review.get("assessed_hash") != article["content_hash"]:
            raise StorageError("review_revision_mismatch", "The review did not match the saved revision.")
        status = "approved" if review["approved"] else "needs_review"
        state = "succeeded" if review["approved"] else "needs_review"
        async with self.pool.connection() as connection, connection.transaction():
            await self._owned(connection, claim.run["owner_id"], article["subject_run_id"], lock=True)
            current = await self._fence(connection, claim)
            if current["review_revision_id"] != _uuid(article["id"]):
                raise StorageError("review_revision_mismatch", "The review subject changed.")
            result = await (await connection.execute(
                "INSERT INTO revision_assessments(review_run_id,revision_id,content_hash,review_status,review,claims) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (_uuid(claim.run_id), _uuid(article["id"]), article["content_hash"], status,
                 Jsonb(review), Jsonb(claims)),
            )).fetchone()
            await connection.execute(
                "UPDATE runs SET state=%s,updated_at=clock_timestamp(),error_code=NULL,error_message=NULL WHERE id=%s",
                (state, _uuid(claim.run_id)),
            )
            await self._event(connection, claim.run_id, "completed", {
                "state": state, "assessed_revision_id": article["id"], "review_status": status,
            }, "terminal")
            await self._event(connection, article["subject_run_id"], "revision_reviewed", {
                "revision_id": article["id"], "review_status": status, "review_run_id": claim.run_id,
            }, f"review:{claim.run_id}")
            await connection.execute(
                "UPDATE jobs SET lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s", (_uuid(claim.run_id),),
            )
            return _public(result)

    async def _fence(
        self,
        connection: Any,
        claim: Claim,
        *,
        allow_cancel: bool = False,
        allow_deadline: bool = False,
    ) -> dict:
        if claim.connection.closed:
            raise StorageError("lease_lost", "Execution ownership was lost")
        row = await (
            await connection.execute(
                "SELECT r.*,j.lease_owner,j.lease_expires_at,j.generation,"
                "j.lease_expires_at>clock_timestamp() AS lease_valid,"
                "r.deadline_at>clock_timestamp() AS deadline_valid "
                "FROM runs r JOIN jobs j ON j.run_id=r.id WHERE r.id=%s FOR UPDATE OF r",
                (_uuid(claim.run_id),),
            )
        ).fetchone()
        if (
            row is None
            or row["lease_owner"] != claim.worker_id
            or row["generation"] != claim.generation
            or not row["lease_valid"]
        ):
            raise StorageError("lease_lost", "Execution ownership was lost")
        unsigned_key = claim.advisory_key & ((1 << 64) - 1)
        lock = await (
            await connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=%s "
                "AND classid=%s::oid AND objid=%s::oid AND objsubid=1 AND granted) AS held",
                (claim.backend_pid, unsigned_key >> 32, unsigned_key & ((1 << 32) - 1)),
            )
        ).fetchone()
        if not lock["held"]:
            raise StorageError("lease_lost", "Execution database session was lost")
        allowed = {"running", "cancel_requested"} if allow_cancel else {"running"}
        if row["state"] not in allowed or (row["deleted_at"] is not None and not allow_cancel):
            raise StorageError("run_cancelled", "This run is no longer accepting generated work")
        if not row["deadline_valid"] and not allow_deadline:
            raise StorageError("deadline_exceeded", "The run deadline has expired")
        return row

    async def claim_job(self, worker_id: str, *, lease_seconds: int = 90) -> Claim | None:
        if not worker_id or len(worker_id) > 255 or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid worker identity or lease duration")
        await self.expire_waiting()
        connection = await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
            # Session locks require a persistent PostgreSQL session, not a
            # transaction-pooled proxy or transparent reconnecting wrapper.
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        claimed = False
        try:
            async with connection.transaction():
                candidate = await (
                    await connection.execute(
                        "SELECT r.*,j.attempt,j.max_attempts FROM jobs j JOIN runs r ON r.id=j.run_id "
                        "WHERE r.state=ANY(%s) AND j.due_at<=clock_timestamp() "
                        "AND (j.lease_owner IS NULL OR j.lease_expires_at<=clock_timestamp()) "
                        "ORDER BY j.due_at,r.accepted_at,r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
                        (list(NONTERMINAL_STATES),),
                    )
                ).fetchone()
                if candidate is None:
                    return None
                run_id = candidate["id"]
                lock_key = int.from_bytes(
                    hashlib.sha256(str(run_id).encode()).digest()[:8], "big", signed=True
                )
                locked = await (
                    await connection.execute(
                        "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,)
                    )
                ).fetchone()
                assert locked is not None
                if not locked["acquired"]:
                    return None
                if candidate["state"] == "cancel_requested" or candidate["deleted_at"] is not None:
                    await connection.execute(
                        "UPDATE runs SET state='cancelled' WHERE id=%s", (run_id,)
                    )
                    await self._event(
                        connection, run_id, "cancelled", {"state": "cancelled"}, "cancelled"
                    )
                    return None
                expired = await (
                    await connection.execute(
                        "SELECT deadline_at<=clock_timestamp() AS expired FROM runs WHERE id=%s",
                        (run_id,),
                    )
                ).fetchone()
                assert expired is not None
                error = None
                if expired["expired"]:
                    error = "deadline_exceeded"
                elif candidate["attempt"] >= candidate["max_attempts"]:
                    error = "attempts_exhausted"
                elif candidate["graph_version"] != self.graph_version:
                    error = "graph_version_mismatch"
                if error:
                    await connection.execute(
                        "UPDATE runs SET state='failed',error_code=%s,error_message=%s WHERE id=%s",
                        (error, "This run could not be resumed. Start a new run.", run_id),
                    )
                    await self._event(
                        connection, run_id, "failed", {"state": "failed", "code": error}, "terminal"
                    )
                    return None
                lease = await (
                    await connection.execute(
                        "UPDATE jobs SET lease_owner=%s,lease_expires_at=clock_timestamp()+(%s*interval '1 second'),"
                        "generation=generation+1,attempt=attempt+1,last_error=NULL WHERE run_id=%s RETURNING generation",
                        (worker_id, lease_seconds, run_id),
                    )
                ).fetchone()
                assert lease is not None
                row = await (
                    await connection.execute(
                        "UPDATE runs SET state='running',updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                        (run_id,),
                    )
                ).fetchone()
                await self._event(
                    connection,
                    run_id,
                    "running",
                    {"state": "running", "attempt": candidate["attempt"] + 1},
                    f"running:{lease['generation']}",
                )
            assert row is not None
            row["attempt"] = candidate["attempt"] + 1
            row["resumed"] = candidate["state"] in {"running", "retry_wait"}
            claimed = True
            return Claim(
                connection,
                _public(row),
                worker_id,
                lease["generation"],
                lease_seconds,
                lock_key,
                connection.info.backend_pid,
            )
        finally:
            if not claimed:
                await connection.close()

    async def expire_waiting(self, *, limit: int = 100) -> int:
        """Expire queued/retry work even when its next retry is not due yet."""
        async with self.pool.connection() as connection, connection.transaction():
            rows = await (
                await connection.execute(
                    "SELECT id FROM runs WHERE state IN ('queued','retry_wait') "
                    "AND deadline_at<=clock_timestamp() ORDER BY deadline_at LIMIT %s FOR UPDATE SKIP LOCKED",
                    (limit,),
                )
            ).fetchall()
            for row in rows:
                await connection.execute(
                    "UPDATE runs SET state='failed',error_code='deadline_exceeded',"
                    "error_message='The run deadline expired before execution completed.' WHERE id=%s",
                    (row["id"],),
                )
                await self._event(
                    connection,
                    row["id"],
                    "failed",
                    {"state": "failed", "code": "deadline_exceeded"},
                    "terminal",
                )
            return len(rows)

    async def activate_due_retries(self, *, limit: int = 100) -> int:
        """Make due retry waits runnable without claiming them in the scheduler."""
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid activation batch")
        async with self.pool.connection() as connection, connection.transaction():
            rows = await (
                await connection.execute(
                    "SELECT r.id,j.generation FROM runs r JOIN jobs j ON j.run_id=r.id "
                    "WHERE r.state='retry_wait' AND j.due_at<=clock_timestamp() "
                    "ORDER BY j.due_at LIMIT %s FOR UPDATE OF r SKIP LOCKED",
                    (limit,),
                )
            ).fetchall()
            for row in rows:
                await connection.execute(
                    "UPDATE runs SET state='queued',updated_at=clock_timestamp() WHERE id=%s",
                    (row["id"],),
                )
                await self._event(
                    connection,
                    row["id"],
                    "queued",
                    {"state": "queued", "resumed": True},
                    f"retry_ready:{row['generation']}",
                )
            return len(rows)

    async def heartbeat(self, claim: Claim) -> bool:
        async with self.pool.connection() as connection, connection.transaction():
            try:
                await self._fence(connection, claim, allow_cancel=True, allow_deadline=True)
            except StorageError:
                return False
            await connection.execute(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()+(%s*interval '1 second') WHERE run_id=%s",
                (claim.lease_seconds, _uuid(claim.run_id)),
            )
            return True

    async def check_active(self, claim: Claim) -> None:
        """Fence before a worker node/dispatch; returns no mutable run object."""
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)

    async def worker_heartbeat(
        self,
        worker_id: str,
        *,
        draining: bool = False,
        active_run_id: str | None = None,
        active_count: int = 0,
    ) -> None:
        if not 0 <= active_count <= 64:
            raise ValueError("Invalid active worker count")
        async with self.pool.connection() as connection:
            await connection.execute(
                "INSERT INTO worker_heartbeats(id,graph_version,draining,active_run_id,active_count) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET graph_version=EXCLUDED.graph_version,"
                "draining=EXCLUDED.draining,active_run_id=EXCLUDED.active_run_id,"
                "active_count=EXCLUDED.active_count,last_seen=clock_timestamp()",
                (
                    worker_id,
                    self.graph_version,
                    draining,
                    _uuid(active_run_id) if active_run_id else None,
                    active_count,
                ),
            )

    async def workers(self, *, stale_seconds: int = 60) -> list[dict]:
        """Registry view including idle, active, draining and dead instances."""
        if not 5 <= stale_seconds <= 3600:
            raise ValueError("Invalid worker freshness bound")
        async with self.pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT id,graph_version,draining,active_run_id,active_count,started_at,last_seen,"
                    "last_seen>clock_timestamp()-(%s*interval '1 second') AS alive,"
                    "extract(epoch from clock_timestamp()-last_seen) AS age_seconds "
                    "FROM worker_heartbeats ORDER BY last_seen DESC",
                    (stale_seconds,),
                )
            ).fetchall()
            return _public(rows)

    async def dead_workers(self, *, stale_seconds: int = 60) -> list[dict]:
        return [row for row in await self.workers(stale_seconds=stale_seconds) if not row["alive"]]

    async def metrics(self, *, stale_seconds: int = 60) -> dict[str, Any]:
        """Bounded aggregate metrics; no run/worker identifiers become labels."""
        if not 5 <= stale_seconds <= 3600:
            raise ValueError("Invalid worker freshness bound")
        async with self.pool.connection() as connection:
            states = {
                row["state"]: row["count"]
                for row in await (
                    await connection.execute(
                        "SELECT state,count(*) AS count FROM runs WHERE deleted_at IS NULL GROUP BY state"
                    )
                ).fetchall()
            }
            queue = await (
                await connection.execute(
                    "SELECT count(*) FILTER (WHERE state IN ('queued','retry_wait')) AS waiting,"
                    "count(*) FILTER (WHERE state='running') AS running,"
                    "count(*) FILTER (WHERE state='cancel_requested') AS cancelling,"
                    "coalesce(max(extract(epoch from clock_timestamp()-accepted_at)) "
                    "FILTER (WHERE state IN ('queued','retry_wait')),0) AS oldest_wait_seconds,"
                    "count(*) FILTER (WHERE deadline_at<=clock_timestamp() AND state IN "
                    "('queued','retry_wait','running','cancel_requested')) AS overdue "
                    "FROM runs WHERE deleted_at IS NULL"
                )
            ).fetchone()
            leases = await (
                await connection.execute(
                    "SELECT count(*) AS expired FROM jobs j JOIN runs r ON r.id=j.run_id "
                    "WHERE j.lease_owner IS NOT NULL AND j.lease_expires_at<=clock_timestamp() "
                    "AND r.state IN ('running','cancel_requested') AND r.deleted_at IS NULL"
                )
            ).fetchone()
            workers = await (
                await connection.execute(
                    "SELECT count(*) FILTER (WHERE last_seen>clock_timestamp()-(%s*interval '1 second') "
                    "AND NOT draining) AS alive,"
                    "count(*) FILTER (WHERE last_seen>clock_timestamp()-(%s*interval '1 second') "
                    "AND draining) AS draining,"
                    "count(*) FILTER (WHERE last_seen<=clock_timestamp()-(%s*interval '1 second')) AS dead,"
                    "coalesce(sum(active_count),0) AS active_runs "
                    "FROM worker_heartbeats",
                    (stale_seconds, stale_seconds, stale_seconds),
                )
            ).fetchone()
            calls = {
                row["status"]: row["count"]
                for row in await (
                    await connection.execute(
                        "SELECT status,count(*) AS count FROM provider_calls "
                        "WHERE created_at>clock_timestamp()-interval '24 hours' GROUP BY status"
                    )
                ).fetchall()
            }
            spend = await (
                await connection.execute(
                    "SELECT coalesce(sum(coalesce(actual_usd,amount_usd)),0) AS reserved_or_known,"
                    "coalesce(sum(coalesce(actual_usd,amount_usd)) FILTER (WHERE actual_usd IS NULL),0) AS unknown,"
                    "count(*) FILTER (WHERE actual_usd IS NULL) AS unknown_count "
                    "FROM budget_reservations WHERE created_at>=date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
                )
            ).fetchone()
            events = await (
                await connection.execute(
                    "SELECT count(*) AS total FROM run_events WHERE created_at>clock_timestamp()-interval '24 hours'"
                )
            ).fetchone()
            assert (
                queue is not None
                and leases is not None
                and workers is not None
                and spend is not None
                and events is not None
            )
            return {
                "states": states,
                "queue": dict(queue),
                "expired_leases": leases["expired"],
                "workers": dict(workers),
                "provider_calls_24h": calls,
                "spend_today": {
                    "reserved_or_known_usd": str(spend["reserved_or_known"]),
                    "unknown_usd": str(spend["unknown"]),
                    "unknown_count": spend["unknown_count"],
                },
                "events_24h": events["total"],
            }

    async def purge_expired(
        self,
        *,
        event_retention_days: int,
        source_retention_days: int,
        run_retention_days: int,
        deleted_grace_hours: int,
        batch_size: int = 100,
        delete_checkpoint: Any = None,
    ) -> dict[str, int]:
        """Bounded retention. Active ownership is never touched."""
        for value, low, high in (
            (event_retention_days, 1, 365),
            (source_retention_days, 1, 365),
            (run_retention_days, 1, 365),
            (deleted_grace_hours, 1, 720),
            (batch_size, 1, 1000),
        ):
            if not low <= value <= high:
                raise ValueError("Invalid retention bound")
        removed = {"events": 0, "sources": 0, "deleted_runs": 0, "retained_runs": 0, "keys": 0}
        async with self.pool.connection() as connection, connection.transaction():
            result = await connection.execute(
                "DELETE FROM run_events WHERE ctid IN (SELECT ctid FROM run_events "
                "WHERE created_at<clock_timestamp()-(%s*interval '1 day') LIMIT %s)",
                (event_retention_days, batch_size),
            )
            removed["events"] = result.rowcount
            old_sources = await (
                await connection.execute(
                    "SELECT r.id FROM runs r WHERE r.state=ANY(%s) AND r.updated_at<"
                    "clock_timestamp()-(%s*interval '1 day') AND EXISTS "
                    "(SELECT 1 FROM sources s WHERE s.run_id=r.id) AND NOT EXISTS "
                    "(SELECT 1 FROM runs q JOIN article_revisions a ON a.id=q.review_revision_id "
                    "WHERE a.run_id=r.id AND q.state=ANY(%s)) "
                    "ORDER BY r.updated_at LIMIT %s FOR UPDATE SKIP LOCKED",
                    (list(TERMINAL_STATES), source_retention_days, list(NONTERMINAL_STATES), batch_size),
                )
            ).fetchall()
            for row in old_sources:
                deleted = await connection.execute(
                    "DELETE FROM sources WHERE run_id=%s", (row["id"],)
                )
                removed["sources"] += deleted.rowcount
            expired_keys = await connection.execute(
                "DELETE FROM idempotency_keys WHERE expires_at<=clock_timestamp() "
                "AND run_id IN (SELECT id FROM runs WHERE state=ANY(%s))",
                (list(TERMINAL_STATES),),
            )
            removed["keys"] = expired_keys.rowcount
        for kind, cutoff, count_key in (
            ("deleted", f"clock_timestamp()-({deleted_grace_hours}*interval '1 hour')", "deleted_runs"),
            ("retained", f"clock_timestamp()-({run_retention_days}*interval '1 day')", "retained_runs"),
        ):
            candidates = await self._retention_candidates(kind, cutoff, batch_size)
            for run_id in candidates:
                if delete_checkpoint is not None:
                    await delete_checkpoint(run_id)
                async with self.pool.connection() as connection, connection.transaction():
                    await connection.execute(
                        "DELETE FROM idempotency_keys WHERE run_id=%s", (_uuid(run_id),)
                    )
                    result = await connection.execute(
                        "DELETE FROM runs r WHERE id=%s AND state=ANY(%s) "
                        + (f"AND deleted_at<{cutoff} " if kind == "deleted" else f"AND updated_at<{cutoff} ")
                        + "AND NOT EXISTS (SELECT 1 FROM runs q JOIN article_revisions a ON a.id=q.review_revision_id "
                        "WHERE a.run_id=r.id AND q.state=ANY(%s))",
                        (_uuid(run_id), list(TERMINAL_STATES), list(NONTERMINAL_STATES)),
                    )
                    removed[count_key] += result.rowcount
        return removed

    async def _retention_candidates(self, kind: str, cutoff: str, limit: int) -> list[str]:
        if kind not in {"deleted", "retained"}:
            raise ValueError("Invalid retention candidate class")
        condition = "deleted_at IS NOT NULL AND deleted_at<" if kind == "deleted" else "deleted_at IS NULL AND updated_at<"
        async with self.pool.connection() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT id FROM runs r WHERE state=ANY(%s) AND {condition}{cutoff} "
                    "AND NOT EXISTS (SELECT 1 FROM runs q JOIN article_revisions a ON a.id=q.review_revision_id "
                    "WHERE a.run_id=r.id AND q.state=ANY(%s)) "
                    "ORDER BY updated_at LIMIT %s",
                    (list(TERMINAL_STATES), list(NONTERMINAL_STATES), limit),
                )
            ).fetchall()
            return [str(row["id"]) for row in rows]

    async def ready(self, *, require_worker: bool = True, freshness_seconds: int = 60) -> bool:
        async with self.pool.connection() as connection:
            await connection.execute("SELECT 1 FROM schema_migrations LIMIT 1")
            if not require_worker:
                return True
            row = await (
                await connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM worker_heartbeats WHERE NOT draining AND graph_version=%s "
                    "AND last_seen>clock_timestamp()-(%s*interval '1 second')) AS fresh",
                    (self.graph_version, freshness_seconds),
                )
            ).fetchone()
            assert row is not None
            return bool(row["fresh"])

    async def append_event(
        self, claim: Claim, event_type: str, payload: dict, *, event_key: str | None = None
    ) -> dict:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            return _public(
                await self._event(
                    connection,
                    claim.run_id,
                    event_type,
                    payload,
                    event_key or f"{event_type}:{canonical_hash(payload)}",
                )
            )

    async def save_plan(self, claim: Claim, plan: dict, *, revision: int = 0) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            row = await (
                await connection.execute(
                    "SELECT plan FROM plans WHERE run_id=%s AND revision=%s",
                    (_uuid(claim.run_id), revision),
                )
            ).fetchone()
            if row:
                if row["plan"] != plan:
                    raise StorageError("plan_conflict", "A committed plan has conflicting contents")
                return
            await connection.execute(
                "INSERT INTO plans(run_id,revision,plan) VALUES (%s,%s,%s)",
                (_uuid(claim.run_id), revision, Jsonb(plan)),
            )
            await self._event(
                connection,
                claim.run_id,
                "plan_ready",
                {"revision": revision, "tasks": len(plan.get("tasks", []))},
                f"plan:{revision}",
            )

    async def save_policy(self, claim: Claim, policy: dict) -> None:
        """Persist the effective deterministic research decision."""
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            await connection.execute(
                "UPDATE runs SET policy=%s,updated_at=clock_timestamp() WHERE id=%s",
                (Jsonb(policy), _uuid(claim.run_id)),
            )
            await self._event(
                connection,
                claim.run_id,
                "policy_ready",
                {
                    "effective_mode": str(policy.get("effective_mode", ""))[:20],
                    "reason": str(policy.get("reason", ""))[:240],
                    "need_research": bool(policy.get("need_research")),
                },
                "policy",
            )

    async def save_research(self, claim: Claim, limitations: list[str], source_count: int) -> None:
        """Persist material research limitations before drafting begins."""
        bounded = [str(item)[:500] for item in limitations][:20]
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            await connection.execute(
                "UPDATE runs SET research_limitations=%s,updated_at=clock_timestamp() WHERE id=%s",
                (Jsonb(bounded), _uuid(claim.run_id)),
            )
            await self._event(
                connection,
                claim.run_id,
                "research_completed",
                {"source_count": source_count, "limitation_count": len(bounded)},
                "research",
            )

    async def save_sources(
        self, claim: Claim, sources: list[dict], excerpts: list[dict] | None = None
    ) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            for source in sources:
                source_id = str(source.get("id") or source.get("source_id"))
                if source_id == "None":
                    raise StorageError("invalid_source", "Each source needs an identity", 422)
                existing = await (
                    await connection.execute(
                        "SELECT source_data FROM sources WHERE run_id=%s AND source_id=%s",
                        (_uuid(claim.run_id), source_id),
                    )
                ).fetchone()
                if existing:
                    if existing["source_data"] != source:
                        raise StorageError(
                            "source_conflict", "A source identity has conflicting contents"
                        )
                else:
                    await connection.execute(
                        "INSERT INTO sources(run_id,source_id,source_data) VALUES (%s,%s,%s)",
                        (_uuid(claim.run_id), source_id, Jsonb(source)),
                    )
                    await self._event(
                        connection,
                        claim.run_id,
                        "source_added",
                        {"source_id": source_id, "provider": str(source.get("provider", ""))[:80]},
                        f"source:{source_id}",
                    )
            for excerpt in excerpts or []:
                existing = await (
                    await connection.execute(
                        "SELECT excerpt_data FROM evidence_excerpts WHERE run_id=%s AND id=%s",
                        (_uuid(claim.run_id), excerpt["id"]),
                    )
                ).fetchone()
                if existing:
                    if existing["excerpt_data"] != excerpt:
                        raise StorageError(
                            "excerpt_conflict", "An excerpt identity has conflicting contents"
                        )
                else:
                    await connection.execute(
                        "INSERT INTO evidence_excerpts(run_id,id,source_id,excerpt_data) VALUES (%s,%s,%s,%s)",
                        (_uuid(claim.run_id), excerpt["id"], excerpt["source_id"], Jsonb(excerpt)),
                    )

    async def save_section(self, claim: Claim, task_id: str, revision: int, content: dict) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            digest = canonical_hash(content)
            existing = await (
                await connection.execute(
                    "SELECT content_hash FROM section_drafts WHERE run_id=%s AND task_id=%s AND revision=%s",
                    (_uuid(claim.run_id), task_id, revision),
                )
            ).fetchone()
            if existing:
                if existing["content_hash"] != digest:
                    raise StorageError(
                        "section_conflict", "A committed section identity has conflicting contents"
                    )
                return
            await connection.execute(
                "INSERT INTO section_drafts(run_id,task_id,revision,content,content_hash) VALUES (%s,%s,%s,%s,%s)",
                (_uuid(claim.run_id), task_id, revision, Jsonb(content), digest),
            )
            await self._event(
                connection,
                claim.run_id,
                "section_completed",
                {"task_id": task_id, "revision": revision},
                f"section:{task_id}:{revision}",
            )

    async def get_sections(self, run_id: str) -> list[dict]:
        """Internal worker-only operation; never expose without owner checks."""
        async with self.pool.connection() as connection:
            return _public(
                await (
                    await connection.execute(
                        "SELECT * FROM section_drafts WHERE run_id=%s ORDER BY task_id,revision",
                        (_uuid(run_id),),
                    )
                ).fetchall()
            )

    async def finalize(
        self,
        claim: Claim,
        markdown: str,
        *,
        review_status: str = "needs_review",
        issues: list[dict] | None = None,
        citation_map: dict | None = None,
        claims: list[dict] | None = None,
        event_type: str = "completed",
        error_code: str | None = None,
    ) -> dict:
        if (
            review_status not in {"approved", "needs_review", "failed"}
            or not markdown.strip()
            or len(markdown) > 200000
            or event_type not in {"completed", "failed"}
        ):
            raise StorageError("invalid_article", "Invalid article output", 422)
        state = {"approved": "succeeded", "needs_review": "needs_review", "failed": "failed"}[
            review_status
        ]
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            revision_id = uuid4()
            row = await (
                await connection.execute(
                    "INSERT INTO article_revisions(id,run_id,markdown,content_hash,review_status,issues,citation_map,author_type) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'model') RETURNING *",
                    (
                        revision_id,
                        _uuid(claim.run_id),
                        markdown,
                        markdown_hash(markdown),
                        review_status,
                        Jsonb(issues or []),
                        Jsonb(citation_map or {}),
                    ),
                )
            ).fetchone()
            article_hash = markdown_hash(markdown)
            await connection.execute(
                "UPDATE runs SET state=%s,current_revision=%s,error_code=%s,"
                "error_message=CASE WHEN %s::text IS NULL THEN error_message ELSE "
                "'This draft could not finish. Review the partial artifact and start again.' END,"
                "updated_at=clock_timestamp() WHERE id=%s",
                (state, revision_id, error_code, error_code, _uuid(claim.run_id)),
            )
            for index, claim_row in enumerate(claims or [], start=1):
                locator = str(claim_row.get("claim_locator") or "")[:500]
                enriched = {
                    **claim_row,
                    "claim_locator": locator,
                    "article_revision_id": str(revision_id),
                    "article_hash": article_hash,
                    "assessor": str(claim_row.get("assessor") or "lexical-v1")[:240],
                    "assessor_version": str(claim_row.get("assessor_version") or "1")[:240],
                }
                await connection.execute(
                    "INSERT INTO claim_citations(revision_id,claim_id,assessment) VALUES (%s,%s,%s)",
                    (revision_id, f"claim_{index}", Jsonb(enriched)),
                )
            await self._event(
                connection,
                claim.run_id,
                event_type,
                {
                    "state": state,
                    "revision_id": str(revision_id),
                    "review_status": review_status,
                    **({"code": error_code} if error_code else {}),
                },
                "terminal",
            )
            await connection.execute(
                "UPDATE jobs SET lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s",
                (_uuid(claim.run_id),),
            )
            return _public(row)

    async def fail_job(self, claim: Claim, code: str, message: str) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            run = await self._fence(connection, claim, allow_cancel=True, allow_deadline=True)
            state = (
                "cancelled"
                if run["state"] == "cancel_requested" or run["deleted_at"] is not None
                else "failed"
            )
            await connection.execute(
                "UPDATE runs SET state=%s,error_code=%s,error_message=%s,updated_at=clock_timestamp() WHERE id=%s",
                (state, code, message, _uuid(claim.run_id)),
            )
            await connection.execute(
                "UPDATE jobs SET last_error=%s WHERE run_id=%s", (code, _uuid(claim.run_id))
            )
            await self._event(
                connection, claim.run_id, state, {"state": state, "code": code}, "terminal"
            )
            await connection.execute(
                "UPDATE jobs SET lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s",
                (_uuid(claim.run_id),),
            )

    async def retry_job(self, claim: Claim, code: str, delay_seconds: float) -> None:
        if not 0 <= delay_seconds <= 86400:
            raise ValueError("Retry delay must be bounded")
        async with self.pool.connection() as connection, connection.transaction():
            await self._fence(connection, claim)
            await connection.execute(
                "UPDATE runs SET state='retry_wait',error_code=%s,updated_at=clock_timestamp() WHERE id=%s",
                (code, _uuid(claim.run_id)),
            )
            await connection.execute(
                "UPDATE jobs SET due_at=clock_timestamp()+(%s*interval '1 second'),lease_owner=NULL,"
                "lease_expires_at=NULL,last_error=%s WHERE run_id=%s",
                (delay_seconds, code, _uuid(claim.run_id)),
            )
            await self._event(
                connection,
                claim.run_id,
                "retry_wait",
                {"state": "retry_wait", "code": code, "delay_seconds": delay_seconds},
                f"retry:{claim.generation}",
            )
