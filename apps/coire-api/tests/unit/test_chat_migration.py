"""Chat persistence guarantees independent of a live control-plane database."""

import runpy
from pathlib import Path

import pytest
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
