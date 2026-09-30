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
    assert "activity_final_state" in tables["chat_turns"].c


def test_registry_backend_defaults_are_populated_on_existing_rows() -> None:
    tables = Base.metadata.tables
    for name in ("models", "model_variants"):
        column = tables[name].c.backend
        assert column.server_default is not None
        assert "mlx_lm" in str(getattr(column.server_default, "arg", ""))
        assert "visual_capability" in tables[name].c


def test_chat_migration_is_one_revision_with_guarded_reverse() -> None:
    source = Path("apps/coire-api/alembic/versions/0015_chat_conversations.py").read_text()
    assert 'revision: str = "0015_chat_conversations"' in source
    assert 'down_revision: str | None = "0014_mcp_calls"' in source
    assert "def upgrade()" in source
    assert "def downgrade()" in source
    assert "chat content must be deleted before downgrade" in source


def test_purge_marker_migration_is_additive_and_reversible() -> None:
    table = Base.metadata.tables["chat_conversations"]
    assert "purged_at" in table.c
    assert any(index.name == "ix_chat_conversations_purge" for index in table.indexes)
    source = Path("apps/coire-api/alembic/versions/0016_chat_purge_marker.py").read_text()
    assert 'down_revision: str | None = "0015_chat_conversations"' in source
    assert "def upgrade()" in source and "def downgrade()" in source


def test_message_context_migration_preserves_old_rows_and_reverses() -> None:
    table = Base.metadata.tables["chat_messages"]
    assert "attachment_selections" in table.c
    assert "prompt_content" in table.c
    source = Path("apps/coire-api/alembic/versions/0017_chat_message_context.py").read_text()
    assert 'down_revision: str | None = "0016_chat_purge_marker"' in source
    assert "'[]'::jsonb" in source
    assert "def upgrade()" in source and "def downgrade()" in source


def test_recovery_mode_migration_is_additive_and_reversible() -> None:
    assert "recovery_mode" in Base.metadata.tables["chat_turns"].c
    source = Path("apps/coire-api/alembic/versions/0018_chat_recovery_mode.py").read_text()
    assert 'down_revision: str | None = "0017_chat_message_context"' in source
    assert "def upgrade()" in source and "def downgrade()" in source


def test_engine_backend_migration_backfills_text_and_is_reversible() -> None:
    column = Base.metadata.tables["engine_processes"].c.backend
    assert column.server_default is not None
    assert "mlx_lm" in str(getattr(column.server_default, "arg", ""))
    source = Path("apps/coire-api/alembic/versions/0019_engine_backend.py").read_text()
    assert 'down_revision: str | None = "0018_chat_recovery_mode"' in source
    assert 'server_default="mlx_lm"' in source
    assert "def upgrade()" in source and "def downgrade()" in source


def test_activity_sequence_migration_is_owned_and_reversible() -> None:
    table = Base.metadata.tables["chat_turns"]
    assert "activity_sequence" in table.c
    assert table.c.activity_sequence.server_default is not None
    assert any(index.name == "uq_chat_turn_run" and index.unique for index in table.indexes)
    source = Path("apps/coire-api/alembic/versions/0020_chat_activity_sequence.py").read_text()
    assert 'down_revision: str | None = "0019_engine_backend"' in source
    assert "def upgrade()" in source and "def downgrade()" in source


@pytest.mark.integration
def test_activity_sequence_backfills_populated_turn_on_local_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    database = "coire_activity_migration_" + uuid.uuid4().hex[:12]
    test_dsn = urlunparse(parsed._replace(path="/" + database))
    model_id, user_id, conversation_id, input_id, answer_id, turn_id = (
        uuid.uuid4() for _ in range(6)
    )

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
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                database,
            )
            await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()

    async def seed_old_turn() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                """INSERT INTO models
                   (id, repo_id, slug, display_name, precision,
                    weight_bytes, total_bytes, file_count, memory_estimate_bytes)
                   VALUES ($1, 'test/activity', 'activity@4bit', 'Activity', '4bit', 1, 1, 1, 1)""",
                model_id,
            )
            await connection.execute(
                "INSERT INTO users (id, email, display_name, role) VALUES ($1, 'activity@example.test', 'Owner', 'user')",
                user_id,
            )
            await connection.execute(
                "INSERT INTO chat_conversations (id, owner_user_id, title) VALUES ($1, $2, 'Code')",
                conversation_id,
                user_id,
            )
            await connection.execute(
                """INSERT INTO chat_messages (id, conversation_id, position, role, text)
                   VALUES ($1, $3, 1, 'user', 'task'), ($2, $3, 2, 'assistant', '')""",
                input_id,
                answer_id,
                conversation_id,
            )
            await connection.execute(
                """INSERT INTO chat_turns
                   (id, conversation_id, client_request_id, request_hash, accepted_revision,
                    input_message_id, assistant_message_id, model_id, model_display_name)
                   VALUES ($1, $2, $3, $4, 1, $5, $6, $7, 'Activity')""",
                turn_id,
                conversation_id,
                uuid.uuid4(),
                "a" * 64,
                input_id,
                answer_id,
                model_id,
            )
        finally:
            await connection.close()

    async def check_upgrade() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert (
                await connection.fetchval(
                    "SELECT activity_sequence FROM chat_turns WHERE id = $1", turn_id
                )
                == 0
            )
            assert await connection.fetchval("SELECT to_regclass('uq_chat_turn_run')") is not None
            assert (
                await connection.fetchval(
                    "SELECT activity_final_state FROM chat_turns WHERE id = $1", turn_id
                )
                is None
            )
        finally:
            await connection.close()

    async def check_reverse() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert (
                await connection.fetchval(
                    """SELECT count(*) FROM information_schema.columns
                       WHERE table_name = 'chat_turns' AND column_name = 'activity_sequence'"""
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    """SELECT count(*) FROM information_schema.columns
                       WHERE table_name = 'chat_turns' AND column_name = 'activity_final_state'"""
                )
                == 0
            )
            assert (
                await connection.fetchval("SELECT id FROM chat_turns WHERE id = $1", turn_id)
                == turn_id
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
        command.upgrade(config, "0019_engine_backend")
        asyncio.run(seed_old_turn())
        command.upgrade(config, "0020_chat_activity_sequence")
        asyncio.run(check_upgrade())
        command.downgrade(config, "0019_engine_backend")
        asyncio.run(check_reverse())
        command.upgrade(config, "0020_chat_activity_sequence")
        asyncio.run(check_upgrade())
    finally:
        asyncio.run(drop_database())


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
def test_stopped_usage_outcome_round_trip_on_disposable_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped usage row blocks reversal; empty enum reversal preserves prior values."""
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
    database = "coire_usage_migration_" + uuid.uuid4().hex[:12]
    test_dsn = urlunparse(parsed._replace(path="/" + database))
    record_id = uuid.uuid4()

    async def setup() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(f"CREATE DATABASE {database}")
        finally:
            await connection.close()

    async def cleanup() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                database,
            )
            await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()

    async def insert_stopped() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                "INSERT INTO usage_records "
                "(id, request_id, principal_kind, requested_model_id, protocol, "
                "prompt_tokens, completion_tokens, duration_ms, outcome, started_at, finished_at) "
                "VALUES ($1, $2, 'admin', 'tiny', 'openai', 1, 1, 1, 'stopped', now(), now())",
                record_id,
                uuid.uuid4(),
            )
        finally:
            await connection.close()

    async def remove_and_check_reverse() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute("DELETE FROM usage_records WHERE id = $1", record_id)
        finally:
            await connection.close()

    async def check_labels(expected_stopped: bool) -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            labels = await connection.fetch(
                "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'usage_outcome'::regtype"
            )
            assert ("stopped" in {row["enumlabel"] for row in labels}) is expected_stopped
        finally:
            await connection.close()

    monkeypatch.setenv("POSTGRES_HOST", parsed.hostname or "localhost")
    monkeypatch.setenv("POSTGRES_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("POSTGRES_USER", unquote(parsed.username or ""))
    monkeypatch.setenv("POSTGRES_PASSWORD", unquote(parsed.password or ""))
    monkeypatch.setenv("POSTGRES_DB", database)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    asyncio.run(setup())
    try:
        command.upgrade(config, "0021_provider_model_targets")
        asyncio.run(check_labels(False))
        command.upgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(check_labels(True))
        asyncio.run(insert_stopped())
        with pytest.raises(RuntimeError, match="export or remove stopped usage records"):
            command.downgrade(config, "0021_provider_model_targets")
        asyncio.run(remove_and_check_reverse())
        command.downgrade(config, "0021_provider_model_targets")
        asyncio.run(check_labels(False))
        command.upgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(check_labels(True))
    finally:
        asyncio.run(cleanup())


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
