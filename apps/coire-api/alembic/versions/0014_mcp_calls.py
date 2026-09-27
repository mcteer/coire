"""MCP call ownership, artifacts, and task-class run admission.

Revision ID: 0014_mcp_calls
Revises: 0013_failover_event_receipts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_mcp_calls"
down_revision: str | None = "0013_failover_event_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("read", "write", name="run_task_class").create(bind, checkfirst=True)
    postgresql.ENUM("research", "plan", "apply", name="mcp_tool_name").create(bind, checkfirst=True)
    postgresql.ENUM(
        "accepted",
        "preparing",
        "queued",
        "running",
        "collecting",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        name="mcp_call_state",
    ).create(bind, checkfirst=True)

    op.add_column(
        "agent_runs",
        sa.Column(
            "task_class",
            _enum("run_task_class", "read", "write"),
            nullable=False,
            server_default="write",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("prepared_request_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("agent_runs", sa.Column("output_ref", sa.String(128), nullable=True))

    op.create_table(
        "mcp_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tool", _enum("mcp_tool_name", "research", "plan", "apply"), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
        ),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column(
            "state",
            _enum(
                "mcp_call_state",
                "accepted",
                "preparing",
                "queued",
                "running",
                "collecting",
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
            ),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_mcp_calls_owner_user_id", "mcp_calls", ["owner_user_id"])
    op.create_index("ix_mcp_calls_state", "mcp_calls", ["state"])

    op.create_table(
        "mcp_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_calls.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_ref", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mcp_artifacts_owner_user_id", "mcp_artifacts", ["owner_user_id"])
    op.create_index("ix_mcp_artifacts_expires_at", "mcp_artifacts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_artifacts_expires_at", table_name="mcp_artifacts")
    op.drop_index("ix_mcp_artifacts_owner_user_id", table_name="mcp_artifacts")
    op.drop_table("mcp_artifacts")
    op.drop_index("ix_mcp_calls_state", table_name="mcp_calls")
    op.drop_index("ix_mcp_calls_owner_user_id", table_name="mcp_calls")
    op.drop_table("mcp_calls")
    op.drop_column("agent_runs", "output_ref")
    op.drop_column("agent_runs", "prepared_request_id")
    op.drop_column("agent_runs", "task_class")
    bind = op.get_bind()
    for name in ("mcp_call_state", "mcp_tool_name", "run_task_class"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
