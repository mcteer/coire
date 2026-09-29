"""Private chat transcript, file processing and backend defaults.

Revision ID: 0015_chat_conversations
Revises: 0014_mcp_calls
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_chat_conversations"
down_revision: str | None = "0014_mcp_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON = postgresql.JSONB()
UTC = sa.DateTime(timezone=True)


def _id() -> sa.Column[uuid.UUID]:
    return sa.Column("id", UUID, primary_key=True)


def _time(name: str, *, nullable: bool = False, default: bool = False) -> sa.Column[datetime]:
    return sa.Column(
        name, UTC, nullable=nullable, server_default=sa.func.now() if default else None
    )


def upgrade() -> None:
    for table in ("models", "model_variants"):
        op.add_column(
            table,
            sa.Column("backend", sa.String(16), nullable=False, server_default="mlx_lm"),
        )
        op.add_column(table, sa.Column("visual_capability", JSON, nullable=True))

    op.create_table(
        "chat_conversations",
        _id(),
        sa.Column(
            "owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False, server_default="chat"),
        sa.Column("selected_model_id", UUID, sa.ForeignKey("models.id", ondelete="SET NULL")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_turn_id", UUID),
        sa.Column("event_cursor", sa.BigInteger(), nullable=False, server_default="0"),
        _time("created_at", default=True),
        _time("updated_at", default=True),
        _time("deleted_at", nullable=True),
        sa.CheckConstraint("revision >= 1", name="ck_chat_conversation_revision"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_chat_conversation_id_owner"),
    )
    op.create_index(
        "ix_chat_conversations_owner_updated",
        "chat_conversations",
        ["owner_user_id", "updated_at", "id"],
    )

    op.create_table(
        "chat_messages",
        _id(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("model_id", UUID, sa.ForeignKey("models.id", ondelete="SET NULL")),
        sa.Column("model_display_name", sa.String(120)),
        sa.Column("attachment_ids", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("usage", JSON),
        _time("created_at", default=True),
        sa.UniqueConstraint("conversation_id", "position", name="uq_chat_message_position"),
        sa.CheckConstraint("position >= 1", name="ck_chat_message_position"),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])

    op.create_table(
        "chat_turns",
        _id(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_request_id", UUID, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("accepted_revision", sa.Integer(), nullable=False),
        sa.Column("input_message_id", UUID, sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("assistant_message_id", UUID, sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column(
            "model_id", UUID, sa.ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("model_display_name", sa.String(120), nullable=False),
        sa.Column("action", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("state", sa.String(24), nullable=False, server_default="accepted"),
        sa.Column("event_cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("retry_of", UUID, sa.ForeignKey("chat_turns.id")),
        sa.Column("coding_call_id", UUID, sa.ForeignKey("mcp_calls.id", ondelete="SET NULL")),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("owner_process", sa.String(128)),
        _time("lease_expires_at", nullable=True),
        sa.Column("stop_reason", sa.String(32)),
        sa.Column("usage", JSON),
        sa.Column("failure_code", sa.String(64)),
        _time("created_at", default=True),
        _time("updated_at", default=True),
        _time("finished_at", nullable=True),
        sa.UniqueConstraint("conversation_id", "client_request_id", name="uq_chat_turn_request"),
        sa.CheckConstraint("accepted_revision >= 1", name="ck_chat_turn_revision"),
    )
    op.create_index("ix_chat_turns_conversation_id", "chat_turns", ["conversation_id"])
    op.create_index(
        "uq_chat_turn_active",
        "chat_turns",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('accepted', 'queued', 'loading', 'running', 'stop_requested')"
        ),
    )
    op.create_foreign_key(
        "fk_chat_conversation_active_turn",
        "chat_conversations",
        "chat_turns",
        ["active_turn_id"],
        ["id"],
    )

    op.create_table(
        "chat_events",
        _id(),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_id", UUID, sa.ForeignKey("chat_turns.id", ondelete="CASCADE")),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        _time("created_at", default=True),
        _time("expires_at"),
        sa.UniqueConstraint("conversation_id", "cursor", name="uq_chat_event_cursor"),
    )
    op.create_index("ix_chat_events_conversation_id", "chat_events", ["conversation_id"])
    op.create_index("ix_chat_event_expires", "chat_events", ["expires_at"])

    op.create_table(
        "chat_attachments",
        _id(),
        sa.Column(
            "owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            UUID,
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("detected_type", sa.String(100), nullable=False),
        sa.Column("original_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("original_key", sa.String(128), nullable=False),
        sa.Column("derived_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("extraction_status", sa.String(32)),
        sa.Column("asset_manifest", JSON),
        sa.Column("safe_error", sa.String(500)),
        _time("created_at", default=True),
        _time("updated_at", default=True),
        _time("deleted_at", nullable=True),
        sa.CheckConstraint(
            "original_bytes > 0 AND original_bytes <= 10485760", name="ck_chat_original_bytes"
        ),
        sa.CheckConstraint(
            "derived_bytes >= 0 AND derived_bytes <= 33554432", name="ck_chat_derived_bytes"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "owner_user_id"],
            ["chat_conversations.id", "chat_conversations.owner_user_id"],
            ondelete="CASCADE",
            name="fk_chat_attachment_owner_conversation",
        ),
    )
    op.create_index(
        "ix_chat_attachment_owner_conversation",
        "chat_attachments",
        ["owner_user_id", "conversation_id"],
    )

    op.create_table(
        "chat_file_processing",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("attachment_id", UUID, sa.ForeignKey("chat_attachments.id", ondelete="CASCADE")),
        sa.Column("owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("principal_kind", sa.String(16), nullable=False),
        sa.Column("principal_subject", sa.String(128), nullable=False),
        sa.Column("request_id", UUID, nullable=False, unique=True),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("selected_pages", JSON, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("output_manifest", JSON),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        _time("deadline_at"),
        _time("expires_at"),
        sa.Column("safe_error", sa.String(500)),
        _time("created_at", default=True),
        _time("updated_at", default=True),
        sa.CheckConstraint("id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'", name="ck_chat_file_job_ulid"),
    )
    op.create_index("ix_chat_file_job_expiry", "chat_file_processing", ["expires_at"])

    op.create_table(
        "chat_quota_reservations",
        _id(),
        sa.Column(
            "owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            UUID,
            nullable=False,
        ),
        sa.Column("attachment_id", UUID, sa.ForeignKey("chat_attachments.id", ondelete="CASCADE")),
        sa.Column(
            "job_id", sa.String(26), sa.ForeignKey("chat_file_processing.id", ondelete="CASCADE")
        ),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        _time("expires_at"),
        _time("created_at", default=True),
        sa.CheckConstraint("reserved_bytes > 0", name="ck_chat_reserved_bytes"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "owner_user_id"],
            ["chat_conversations.id", "chat_conversations.owner_user_id"],
            ondelete="CASCADE",
            name="fk_chat_quota_owner_conversation",
        ),
    )
    op.create_index(
        "ix_chat_quota_owner_state", "chat_quota_reservations", ["owner_user_id", "state"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "chat_quota_reservations",
        "chat_file_processing",
        "chat_attachments",
        "chat_events",
        "chat_turns",
        "chat_messages",
        "chat_conversations",
    ):
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")):
            raise RuntimeError("chat content must be deleted before downgrade")

    op.drop_table("chat_quota_reservations")
    op.drop_table("chat_file_processing")
    op.drop_table("chat_attachments")
    op.drop_table("chat_events")
    op.drop_constraint("fk_chat_conversation_active_turn", "chat_conversations", type_="foreignkey")
    op.drop_table("chat_turns")
    op.drop_table("chat_messages")
    op.drop_table("chat_conversations")
    for table in ("model_variants", "models"):
        op.drop_column(table, "visual_capability")
        op.drop_column(table, "backend")
