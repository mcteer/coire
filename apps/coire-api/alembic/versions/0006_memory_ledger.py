"""Authoritative placement memory ledger.

Revision ID: 0006_memory_ledger
Revises: 0005_acquisition_variants
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_memory_ledger"
down_revision: str | None = "0005_acquisition_variants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    enums = {
        "reservation_holder": ("sandbox", "model", "conversion", "training", "image", "run"),
        "memory_reservation_state": ("pending", "held", "releasing", "released", "failed"),
        "placement_state": (
            "requested",
            "waiting_for_drain",
            "evicting",
            "reserving",
            "loading",
            "ready",
            "refused",
            "failed",
        ),
    }
    for name, values in enums.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "node_memory_ledgers",
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("budget_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sandbox_bytes", sa.BigInteger(), nullable=False, server_default="17179869184"),
        sa.Column("measured_resident_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("thermal_state", sa.String(16), nullable=True),
        sa.Column(
            "health",
            _enum("reachability", ("healthy", "degraded", "unreachable", "unknown")),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("health_reason", sa.Text(), nullable=True),
        sa.Column("health_sampled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "INSERT INTO node_memory_ledgers (node_id, budget_bytes, sandbox_bytes, health) "
        "SELECT id, 246960619520, 17179869184, reachability FROM nodes"
    )
    op.create_table(
        "memory_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "holder_type", _enum("reservation_holder", enums["reservation_holder"]), nullable=False
        ),
        sa.Column("holder_id", sa.String(255), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "state",
            _enum("memory_reservation_state", enums["memory_reservation_state"]),
            nullable=False,
        ),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "node_id", "holder_type", "holder_id", name="uq_memory_reservation_holder"
        ),
    )
    op.create_index("ix_memory_reservations_node_id", "memory_reservations", ["node_id"])
    op.create_index("ix_memory_reservations_state", "memory_reservations", ["state"])
    op.create_index("ix_memory_reservations_last_used_at", "memory_reservations", ["last_used_at"])
    op.execute(
        "INSERT INTO memory_reservations "
        "(id, node_id, holder_type, holder_id, bytes, pinned, state) "
        "SELECT node_id, node_id, 'sandbox', 'agent-sandbox', sandbox_bytes, true, 'held' "
        "FROM node_memory_ledgers WHERE sandbox_bytes > 0"
    )
    op.create_table(
        "request_leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_reservations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(128), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_request_leases_reservation_id", "request_leases", ["reservation_id"])
    op.create_index("ix_request_leases_expires_at", "request_leases", ["expires_at"])
    op.create_table(
        "placement_decisions",
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
        sa.Column("policy", sa.String(64), nullable=False),
        sa.Column("required_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", _enum("placement_state", enums["placement_state"]), nullable=False),
        sa.Column(
            "selected_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id"),
            nullable=True,
        ),
        sa.Column(
            "evicted_reservation_ids", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("refusal_code", sa.String(64), nullable=True),
        sa.Column("refusal_detail", sa.Text(), nullable=True),
        sa.Column("occupants", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_placement_decisions_model_id", "placement_decisions", ["model_id"])
    op.create_index("ix_placement_decisions_variant_id", "placement_decisions", ["variant_id"])
    op.create_index("ix_placement_decisions_state", "placement_decisions", ["state"])
    op.create_table(
        "eviction_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("placement_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("nodes.id"), nullable=False
        ),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_reservations.id"),
            nullable=False,
        ),
        sa.Column("lru_rank", sa.Integer(), nullable=False),
        sa.Column("skipped", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_eviction_events_decision_id", "eviction_events", ["decision_id"])
    op.create_table(
        "placement_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("engine_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["decision_id"], ["placement_decisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["memory_reservations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_placement_commands_decision_id", "placement_commands", ["decision_id"])
    op.create_index("ix_placement_commands_state", "placement_commands", ["state"])


def downgrade() -> None:
    for table in (
        "placement_commands",
        "eviction_events",
        "request_leases",
        "placement_decisions",
        "memory_reservations",
        "node_memory_ledgers",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for name in ("placement_state", "memory_reservation_state", "reservation_holder"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
