"""Durable model instances and declared-node credentials.

Revision ID: 0007_model_instances
Revises: 0006_memory_ledger
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_model_instances"
down_revision: str | None = "0006_memory_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _instance_state() -> postgresql.ENUM:
    return postgresql.ENUM(
        "requested",
        "reserving",
        "launching",
        "warming",
        "ready",
        "draining",
        "stopped",
        "failed",
        name="instance_state",
        create_type=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        "requested",
        "reserving",
        "launching",
        "warming",
        "ready",
        "draining",
        "stopped",
        "failed",
        name="instance_state",
    ).create(bind, checkfirst=True)

    for column in (
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registration_token_digest", sa.String(64), nullable=True),
        sa.Column("token_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gpu_percent", sa.Float(), nullable=True),
    ):
        op.add_column("nodes", column)

    op.create_table(
        "model_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_variants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "placement_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("placement_decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("policy", sa.String(64), nullable=False),
        sa.Column("state", _instance_state(), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("in_flight", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("drain_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_model_instances_model_id", "model_instances", ["model_id"])
    op.create_index("ix_model_instances_variant_id", "model_instances", ["variant_id"])
    op.create_index("ix_model_instances_state", "model_instances", ["state"])

    op.add_column(
        "engine_processes",
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_instances.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_engine_processes_instance_id", "engine_processes", ["instance_id"])

    op.create_table(
        "instance_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("engine_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_reservations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.UniqueConstraint("instance_id", "rank", name="uq_instance_member_rank"),
        sa.UniqueConstraint("instance_id", "node_id", name="uq_instance_member_node"),
    )
    op.create_index("ix_instance_members_instance_id", "instance_members", ["instance_id"])

    op.create_table(
        "instance_transitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_state", _instance_state(), nullable=True),
        sa.Column("state", _instance_state(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("instance_id", "sequence", name="uq_instance_transition_sequence"),
    )
    op.create_index("ix_instance_transitions_instance_id", "instance_transitions", ["instance_id"])
    op.create_table(
        "registration_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("node_name", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column("remote_identity", sa.String(255), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_registration_attempts_node_name", "registration_attempts", ["node_name"])

    # Preserve legacy engines as instances. Engine UUID is a stable, collision-free instance/member id.
    op.execute(
        """
        INSERT INTO model_instances
          (id, model_id, variant_id, policy, state, created_at, updated_at, transitioned_at)
        SELECT e.id, e.model_id, v.id, 'legacy:migrated',
          CASE
            WHEN e.state::text = 'ready' THEN 'ready'::instance_state
            WHEN e.state::text = 'stopped' THEN 'stopped'::instance_state
            WHEN e.state::text IN ('starting','loading') THEN 'launching'::instance_state
            ELSE 'failed'::instance_state
          END,
          e.started_at, COALESCE(e.stopped_at, now()), COALESCE(e.stopped_at, e.started_at)
        FROM engine_processes e
        JOIN LATERAL (
          SELECT id FROM model_variants
          WHERE model_id=e.model_id
          ORDER BY is_default DESC, created_at
          LIMIT 1
        ) v ON true
        WHERE e.model_id IS NOT NULL
        """
    )
    op.execute("UPDATE engine_processes SET instance_id=id WHERE model_id IS NOT NULL")
    op.execute(
        """
        INSERT INTO instance_members
          (id, instance_id, node_id, rank, engine_id, reservation_id, host, port)
        SELECT e.id, e.id, e.node_id, 0, e.id, r.id,
               COALESCE(n.control_host, n.name), e.port
        FROM engine_processes e
        JOIN nodes n ON n.id=e.node_id
        LEFT JOIN memory_reservations r
          ON r.node_id=e.node_id AND r.holder_type='model'
         AND r.holder_id=e.model_id::text AND r.state IN ('pending','held','releasing')
        WHERE e.instance_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE memory_reservations r SET holder_id=e.instance_id::text
        FROM engine_processes e
        WHERE e.instance_id IS NOT NULL AND r.node_id=e.node_id
          AND r.holder_type='model' AND r.holder_id=e.model_id::text
          AND r.state IN ('pending','held','releasing')
        """
    )
    op.execute(
        """
        INSERT INTO instance_transitions (instance_id, sequence, state, reason, at)
        SELECT id, 1, state, 'migrated from engine process', transitioned_at
        FROM model_instances
        """
    )


def downgrade() -> None:
    # Restore active reservation holder ids before removing instance ownership.
    op.execute(
        """
        UPDATE memory_reservations r SET holder_id=i.model_id::text
        FROM instance_members m JOIN model_instances i ON i.id=m.instance_id
        WHERE r.id=m.reservation_id AND r.holder_type='model'
        """
    )
    op.drop_index("ix_engine_processes_instance_id", table_name="engine_processes")
    op.drop_column("engine_processes", "instance_id")
    for table in (
        "registration_attempts",
        "instance_transitions",
        "instance_members",
        "model_instances",
    ):
        op.drop_table(table)
    for name in (
        "gpu_percent",
        "health_observed_at",
        "token_revoked_at",
        "token_consumed_at",
        "token_issued_at",
        "registration_token_digest",
        "declared_at",
    ):
        op.drop_column("nodes", name)
    postgresql.ENUM(name="instance_state").drop(op.get_bind(), checkfirst=True)
