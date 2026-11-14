"""Run checksummed application migrations and optional LangGraph saver setup.

Use migration credentials, not the restricted runtime role. Never call this in
every API replica's startup path.
"""

import argparse
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

MIGRATION_LOCK = 1195461969


def migration_files(directory: Path) -> list[Path]:
    paths = sorted(
        p for p in directory.glob("*.sql") if re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", p.name)
    )
    if not paths:
        raise RuntimeError(f"No numbered migrations found in {directory}")
    return paths


async def migrate(database_url: str, directory: Path, *, setup_saver: bool = True) -> list[str]:
    """Apply each file transactionally while holding a session migration lock."""
    connection = await psycopg.AsyncConnection.connect(
        database_url, autocommit=True, row_factory=dict_row, connect_timeout=5
    )
    applied: list[str] = []
    try:
        # A blocking SELECT holds a transaction snapshot while waiting for the
        # advisory lock. LangGraph's concurrent index builds can wait for that
        # snapshot, deadlocking the holder against another migration session.
        while True:
            row = await (await connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (MIGRATION_LOCK,),
            )).fetchone()
            if row and row["acquired"]:
                break
            await asyncio.sleep(0.1)
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "id text PRIMARY KEY, checksum text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
        files = migration_files(directory)
        known = {
            row["id"]: row["checksum"]
            for row in await (
                await connection.execute("SELECT id,checksum FROM schema_migrations ORDER BY id")
            ).fetchall()
        }
        if set(known) - {p.name for p in files}:
            raise RuntimeError("Database contains migrations absent from this application")
        for path in files:
            body = path.read_bytes()
            checksum = hashlib.sha256(body).hexdigest()
            if path.name in known:
                if known[path.name] != checksum:
                    raise RuntimeError(f"Migration checksum mismatch: {path.name}")
                continue
            if any(name > path.name for name in known):
                raise RuntimeError(f"Out-of-order migration: {path.name}")
            async with connection.transaction():
                await connection.execute(body.decode("utf-8"), prepare=False)
                await connection.execute(
                    "INSERT INTO schema_migrations(id,checksum) VALUES (%s,%s)",
                    (path.name, checksum),
                )
            applied.append(path.name)
        if setup_saver:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            await AsyncPostgresSaver(connection).setup()
    finally:
        # Closing also releases session advisory locks after any rollback/error.
        await connection.close()
    return applied


async def verify_schema(
    connection: psycopg.AsyncConnection[dict[str, Any]], directory: Path
) -> None:
    """Reject missing, extra, or changed migrations without DDL privileges."""
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in migration_files(directory)
    }
    actual = {
        row["id"]: row["checksum"]
        for row in await (
            await connection.execute("SELECT id,checksum FROM schema_migrations")
        ).fetchall()
    }
    if expected != actual:
        raise RuntimeError("Database schema is incompatible; run the deployment migration step")


async def grant_runtime(database_url: str, role: str) -> None:
    """Grant runtime data access, retaining migration metadata as read-only.

    The role must already exist; passwords and identity creation are deliberately
    outside this command. Run after saver setup so checkpoint tables are included.
    """
    async with await psycopg.AsyncConnection.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        schema_row = await (await connection.execute("SELECT current_schema() AS name")).fetchone()
        assert schema_row is not None
        schema = schema_row["name"]
        async with connection.transaction():
            await connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            tables = await (
                await connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,)
                )
            ).fetchall()
            for table in tables:
                privilege = (
                    sql.SQL("SELECT")
                    if table["tablename"] in {"schema_migrations", "checkpoint_migrations"}
                    else sql.SQL("SELECT, INSERT, UPDATE, DELETE")
                )
                await connection.execute(
                    sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
                        privilege,
                        sql.Identifier(schema),
                        sql.Identifier(table["tablename"]),
                        sql.Identifier(role),
                    )
                )


async def _command(
    database_url: str, directory: Path, application_only: bool, role: str | None
) -> list[str]:
    applied = await migrate(database_url, directory, setup_saver=not application_only)
    if role:
        await grant_runtime(database_url, role)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("migrations"))
    parser.add_argument(
        "--application-only", action="store_true", help="Skip LangGraph saver setup"
    )
    parser.add_argument(
        "--runtime-role", help="Existing restricted role to grant data access after setup"
    )
    args = parser.parse_args()
    database_url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set MIGRATION_DATABASE_URL (or DATABASE_URL) before running migrations")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    applied = asyncio.run(
        _command(database_url, args.directory, args.application_only, args.runtime_role),
        loop_factory=loop_factory,
    )
    print(f"Application migrations applied: {len(applied)}; schema checksums verified")


if __name__ == "__main__":
    main()
