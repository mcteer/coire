"""Durable Studio container runs and server-side run credentials.

Revision ID: 0011_container_runs
Revises: 0010_harness_evaluations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_container_runs"
down_revision: str | None = "0010_harness_evaluations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    enums = {
        "agent_run_state": (
            "queued",
            "placing",
            "creating",
            "running",
            "collecting",
            "succeeded",
            "failed",
            "result_collection_failed",
            "timed_out",
            "kill_requested",
            "killed",
        ),
        "run_operation": (
            "create",
            "start",
            "logs",
            "wait",
            "collect",
            "remove",
            "kill",
            "reconcile",
        ),
        "run_command_state": ("pending", "running", "succeeded", "failed"),
    }
    for name, values in enums.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    run_state = _enum("agent_run_state", *enums["agent_run_state"])
    operation = _enum("run_operation", *enums["run_operation"])
    command_state = _enum("run_command_state", *enums["run_command_state"])
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "requester_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("profile", sa.String(16), nullable=False),
        sa.Column(
            "primary_model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
        ),
        sa.Column("container_id", sa.String(128), unique=True),
        sa.Column("workspace_ref", sa.String(128), nullable=False),
        sa.Column("token_scope", postgresql.JSONB(), nullable=False),
        sa.Column("state", run_state, nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_detail", sa.String(500)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("resource_usage", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "killed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("killed_at", sa.DateTime(timezone=True)),
    )
    for column in ("requester_user_id", "primary_model_id", "node_id", "state"):
        op.create_index(f"ix_agent_runs_{column}", "agent_runs", [column])
    op.create_table(
        "agent_run_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_state", run_state),
        sa.Column("to_state", run_state, nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_run_transitions_run_id", "agent_run_transitions", ["run_id"])
    op.create_table(
        "run_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("prefix", sa.String(12), nullable=False, unique=True),
        sa.Column("secret_hash", sa.String(255), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("spent_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_run_tokens_run_id", "run_tokens", ["run_id"], unique=True)
    op.create_index("ix_run_tokens_prefix", "run_tokens", ["prefix"], unique=True)
    op.create_index("ix_run_tokens_expires_at", "run_tokens", ["expires_at"])
    op.create_table(
        "run_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
        ),
        sa.Column("operation", operation, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", command_state, nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "operation", "attempt", name="uq_run_command_attempt"),
    )
    op.create_index("ix_run_commands_run_id", "run_commands", ["run_id"])
    op.create_index("ix_run_commands_state", "run_commands", ["state"])


def downgrade() -> None:
    op.drop_index("ix_run_commands_state", table_name="run_commands")
    op.drop_index("ix_run_commands_run_id", table_name="run_commands")
    op.drop_table("run_commands")
    op.drop_index("ix_run_tokens_expires_at", table_name="run_tokens")
    op.drop_index("ix_run_tokens_prefix", table_name="run_tokens")
    op.drop_index("ix_run_tokens_run_id", table_name="run_tokens")
    op.drop_table("run_tokens")
    op.drop_index("ix_agent_run_transitions_run_id", table_name="agent_run_transitions")
    op.drop_table("agent_run_transitions")
    for column in reversed(("requester_user_id", "primary_model_id", "node_id", "state")):
        op.drop_index(f"ix_agent_runs_{column}", table_name="agent_runs")
    op.drop_table("agent_runs")
    bind = op.get_bind()
    for name in ("run_command_state", "run_operation", "agent_run_state"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
