"""Measured Studio link and durable sharded serving.

Revision ID: 0008_sharded_serving
Revises: 0007_model_instances
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_sharded_serving"
down_revision: str | None = "0007_model_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    enums = {
        "probe_transport": ("jaccl", "ring"),
        "probe_outcome": ("succeeded", "failed"),
        "sharding_mode": ("tp", "pp"),
        "shard_group_state": ("preparing", "starting", "ready", "stopping", "stopped", "failed"),
        "benchmark_run_state": ("queued", "running", "completed", "failed"),
    }
    for name, values in enums.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)
    op.add_column(
        "instance_members",
        sa.Column("rank_healthy", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "instance_members",
        sa.Column("last_rank_health_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "model_instances",
        sa.Column("fallback_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "model_instances",
        sa.Column("fallback_instance_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_instances",
        sa.Column("fallback_no_fit", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "link_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_a_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_b_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("transport", _enum("probe_transport", "jaccl", "ring"), nullable=False),
        sa.Column("outcome", _enum("probe_outcome", "succeeded", "failed"), nullable=False),
        sa.Column("bandwidth_bytes_per_second", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("os_version_a", sa.String(64), nullable=False),
        sa.Column("os_version_b", sa.String(64), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_link_observations_pair_at",
        "link_observations",
        ["node_a_id", "node_b_id", "observed_at"],
    )
    op.create_table(
        "shard_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_instances.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("mode", _enum("sharding_mode", "tp", "pp"), nullable=False),
        sa.Column(
            "state",
            _enum(
                "shard_group_state",
                "preparing",
                "starting",
                "ready",
                "stopping",
                "stopped",
                "failed",
            ),
            nullable=False,
        ),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("hostfile_sha256", sa.String(64), nullable=False),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_shard_groups_instance_id", "shard_groups", ["instance_id"])
    op.create_table(
        "shard_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shard_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_shard_commands_group_id", "shard_commands", ["group_id"])
    op.create_index("ix_shard_commands_state", "shard_commands", ["state"])
    op.create_table(
        "benchmark_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum("benchmark_run_state", "queued", "running", "completed", "failed"),
            nullable=False,
        ),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("generation_tokens", sa.Integer(), nullable=False),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_benchmark_runs_variant_id", "benchmark_runs", ["variant_id"])
    op.create_index("ix_benchmark_runs_state", "benchmark_runs", ["state"])
    op.create_table(
        "placement_benchmarks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("benchmark_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("placement", sa.String(64), nullable=False),
        sa.Column("tokens_per_second", sa.Float(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("generation_tokens", sa.Integer(), nullable=False),
        sa.Column("gpu_cores", postgresql.JSONB(), nullable=False),
        sa.Column("os_versions", postgresql.JSONB(), nullable=False),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_placement_benchmarks_run_id", "placement_benchmarks", ["run_id"])
    op.create_index("ix_placement_benchmarks_variant_id", "placement_benchmarks", ["variant_id"])
    op.create_table(
        "benchmark_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("benchmark_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_benchmark_commands_run_id", "benchmark_commands", ["run_id"])
    op.create_index("ix_benchmark_commands_state", "benchmark_commands", ["state"])


def downgrade() -> None:
    op.drop_table("benchmark_commands")
    op.drop_table("placement_benchmarks")
    op.drop_table("benchmark_runs")
    op.drop_table("shard_commands")
    op.drop_table("shard_groups")
    op.drop_table("link_observations")
    op.drop_column("instance_members", "last_rank_health_at")
    op.drop_column("instance_members", "rank_healthy")
    op.drop_column("model_instances", "fallback_no_fit")
    op.drop_column("model_instances", "fallback_instance_id")
    op.drop_column("model_instances", "fallback_attempted_at")
    bind = op.get_bind()
    for name in (
        "benchmark_run_state",
        "shard_group_state",
        "sharding_mode",
        "probe_outcome",
        "probe_transport",
    ):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
