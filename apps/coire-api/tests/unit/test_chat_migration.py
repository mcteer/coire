"""Chat persistence guarantees independent of a live control-plane database."""

import asyncio
import os
import runpy
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from coire_api.db import Base


def test_chat_tables_have_owner_and_position_constraints() -> None:
    tables = Base.metadata.tables
    for name in (
        "chat_conversations",
        "chat_messages",
        "chat_turns",
        "chat_events",
        "chat_attachments",
        "chat_file_processing",
        "chat_quota_reservations",
    ):
        assert name in tables
    assert any(
        fk.target_fullname == "users.id"
        for fk in tables["chat_conversations"].c.owner_user_id.foreign_keys
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns} == {"conversation_id", "position"}
        for constraint in tables["chat_messages"].constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns}
        == {"conversation_id", "client_request_id"}
        for constraint in tables["chat_turns"].constraints
    )
    assert any(
        isinstance(index, Index)
        and index.unique
        and index.dialect_options["postgresql"].get("where") is not None
        for index in tables["chat_turns"].indexes
    )
    assert any(
        isinstance(constraint, CheckConstraint) and "revision" in str(constraint.sqltext)
        for constraint in tables["chat_conversations"].constraints
    )


def test_registry_backend_defaults_are_populated_on_existing_rows() -> None:
    tables = Base.metadata.tables
    for name in ("models", "model_variants"):
        column = tables[name].c.backend
        assert column.server_default is not None
        assert "mlx_lm" in str(column.server_default.arg)
        assert "visual_capability" in tables[name].c


def test_chat_migration_is_one_revision_with_guarded_reverse() -> None:
    source = Path("apps/coire-api/alembic/versions/0015_chat_conversations.py").read_text()
    assert 'revision: str = "0015_chat_conversations"' in source
    assert 'down_revision: str | None = "0014_mcp_calls"' in source
    assert "def upgrade()" in source
    assert "def downgrade()" in source
    assert "chat content must be deleted before downgrade" in source


def test_populated_chat_blocks_downgrade_before_any_drop() -> None:
    namespace = runpy.run_path("apps/coire-api/alembic/versions/0015_chat_conversations.py")
    downgrade = namespace["downgrade"]

    class PopulatedBind:
        def scalar(self, statement: object) -> bool:
            return True

    class GuardedOp:
        def get_bind(self) -> PopulatedBind:
            return PopulatedBind()

        def drop_table(self, name: str) -> None:
            raise AssertionError(f"downgrade tried to drop {name}")

    downgrade.__globals__["op"] = GuardedOp()
    with pytest.raises(RuntimeError, match="chat content must be deleted before downgrade"):
        downgrade()


@pytest.mark.integration
def test_populated_upgrade_downgrade_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise 0015 with real constraints and transaction rollback."""
    admin_dsn = os.environ.get("COIRE_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("set COIRE_TEST_POSTGRES_DSN for disposable local PostgreSQL")
    parsed = urlparse(admin_dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("migration test requires an explicit local PostgreSQL server")
    database = "coire_chat_migration_" + uuid.uuid4().hex[:12]
    test_dsn = urlunparse(parsed._replace(path="/" + database))
    model_id, user_id, conversation_id, message_id = (uuid.uuid4() for _ in range(4))

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(f"CREATE DATABASE {database}")
        finally:
            await connection.close()

    async def drop_database() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(
                """SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                   WHERE datname = $1 AND pid <> pg_backend_pid()""",
                database,
            )
            await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()

    async def seed_legacy_rows() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                """INSERT INTO models
                   (id, repo_id, slug, display_name, precision,
                    weight_bytes, total_bytes, file_count, memory_estimate_bytes)
                   VALUES ($1, 'test/legacy', 'legacy@4bit', 'Legacy', '4bit', 1, 1, 1, 1)""",
                model_id,
            )
            await connection.execute(
                """INSERT INTO users (id, email, display_name, role)
                   VALUES ($1, 'chat-migration@example.test', 'Owner', 'user')""",
                user_id,
            )
        finally:
            await connection.close()

    async def populate_chat() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert (
                await connection.fetchval("SELECT backend FROM models WHERE id = $1", model_id)
                == "mlx_lm"
            )
            await connection.execute(
                """INSERT INTO chat_conversations (id, owner_user_id, title, selected_model_id)
                   VALUES ($1, $2, 'Saved', $3)""",
                conversation_id,
                user_id,
                model_id,
            )
            await connection.execute(
                """INSERT INTO chat_messages (id, conversation_id, position, role, text)
                   VALUES ($1, $2, 1, 'user', 'retained content')""",
                message_id,
                conversation_id,
            )
        finally:
            await connection.close()

    async def verify_guard_and_clear() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert (
                await connection.fetchval("SELECT to_regclass('chat_messages')") == "chat_messages"
            )
            assert (
                await connection.fetchval(
                    "SELECT text FROM chat_messages WHERE id = $1", message_id
                )
                == "retained content"
            )
            assert await connection.fetchval("SELECT count(*) FROM chat_conversations") == 1
            await connection.execute("DELETE FROM chat_messages")
            await connection.execute("DELETE FROM chat_conversations")
        finally:
            await connection.close()

    async def verify_reverse() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert await connection.fetchval("SELECT to_regclass('chat_conversations')") is None
            assert (
                await connection.fetchval(
                    """SELECT count(*) FROM information_schema.columns
                   WHERE table_name = 'models' AND column_name = 'backend'"""
                )
                == 0
            )
            assert (
                await connection.fetchval("SELECT display_name FROM models WHERE id = $1", model_id)
                == "Legacy"
            )
        finally:
            await connection.close()

    monkeypatch.setenv("POSTGRES_HOST", parsed.hostname or "localhost")
    monkeypatch.setenv("POSTGRES_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("POSTGRES_USER", unquote(parsed.username or ""))
    monkeypatch.setenv("POSTGRES_PASSWORD", unquote(parsed.password or ""))
    monkeypatch.setenv("POSTGRES_DB", database)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    asyncio.run(create_database())
    try:
        command.upgrade(config, "0014_mcp_calls")
        asyncio.run(seed_legacy_rows())
        command.upgrade(config, "0015_chat_conversations")
        asyncio.run(populate_chat())
        with pytest.raises(RuntimeError, match="chat content must be deleted before downgrade"):
            command.downgrade(config, "0014_mcp_calls")
        asyncio.run(verify_guard_and_clear())
        command.downgrade(config, "0014_mcp_calls")
        asyncio.run(verify_reverse())
        command.upgrade(config, "0015_chat_conversations")
    finally:
        asyncio.run(drop_database())
