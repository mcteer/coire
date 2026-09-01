"""Persist ops conversations and exact-action confirmation authority.

Revision ID: 0012_ops_confirmations
Revises: 0011_container_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_ops_confirmations"
down_revision: str | None = "0011_container_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    enums = {
        "ops_session_state": ("active", "superseded", "expired"),
        "ops_conversation_state": ("active", "closed"),
        "ops_message_role": ("admin", "ops", "system"),
        "ops_proposal_state": (
            "pending",
            "confirmed",
            "executing",
            "executed",
            "declined",
            "expired",
            "stale",
            "failed",
        ),
    }
    for name, values in enums.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "ops_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service_instance", sa.String(128), nullable=False),
        sa.Column("state", _enum("ops_session_state", *enums["ops_session_state"]), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ops_sessions_state", "ops_sessions", ["state"])
    op.create_index("ix_ops_sessions_last_seen_at", "ops_sessions", ["last_seen_at"])
    op.create_table(
        "ops_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "ops_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ops_sessions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "state",
            _enum("ops_conversation_state", *enums["ops_conversation_state"]),
            nullable=False,
        ),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in ("admin_user_id", "ops_session_id", "state"):
        op.create_index(f"ix_ops_conversations_{column}", "ops_conversations", [column])
    op.create_table(
        "ops_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ops_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", _enum("ops_message_role", *enums["ops_message_role"]), nullable=False),
        sa.Column("content", sa.String(4000), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ops_messages_conversation_id", "ops_messages", ["conversation_id"])
    op.create_table(
        "ops_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ops_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ops_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ops_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("proposer", sa.String(128), nullable=False),
        sa.Column("action", postgresql.JSONB(), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column(
            "state", _enum("ops_proposal_state", *enums["ops_proposal_state"]), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "confirmed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("failure_code", sa.String(64)),
    )
    for column in (
        "conversation_id",
        "ops_session_id",
        "state",
        "expires_at",
        "confirmed_by_user_id",
    ):
        op.create_index(f"ix_ops_proposals_{column}", "ops_proposals", [column])
    op.create_table(
        "ops_confirmation_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ops_proposals.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("prefix", sa.String(12), nullable=False, unique=True),
        sa.Column("secret_hash", sa.String(255), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column(
            "issued_to_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in ("proposal_id", "prefix", "issued_to_user_id", "expires_at"):
        op.create_index(
            f"ix_ops_confirmation_tokens_{column}",
            "ops_confirmation_tokens",
            [column],
            unique=column in {"proposal_id", "prefix"},
        )


def downgrade() -> None:
    for column in reversed(("proposal_id", "prefix", "issued_to_user_id", "expires_at")):
        op.drop_index(f"ix_ops_confirmation_tokens_{column}", table_name="ops_confirmation_tokens")
    op.drop_table("ops_confirmation_tokens")
    for column in reversed(
        ("conversation_id", "ops_session_id", "state", "expires_at", "confirmed_by_user_id")
    ):
        op.drop_index(f"ix_ops_proposals_{column}", table_name="ops_proposals")
    op.drop_table("ops_proposals")
    op.drop_index("ix_ops_messages_conversation_id", table_name="ops_messages")
    op.drop_table("ops_messages")
    for column in reversed(("admin_user_id", "ops_session_id", "state")):
        op.drop_index(f"ix_ops_conversations_{column}", table_name="ops_conversations")
    op.drop_table("ops_conversations")
    op.drop_index("ix_ops_sessions_last_seen_at", table_name="ops_sessions")
    op.drop_index("ix_ops_sessions_state", table_name="ops_sessions")
    op.drop_table("ops_sessions")
    bind = op.get_bind()
    for name in (
        "ops_proposal_state",
        "ops_message_role",
        "ops_conversation_state",
        "ops_session_state",
    ):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
