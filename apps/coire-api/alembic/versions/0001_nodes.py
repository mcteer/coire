"""Create the nodes table.

Revision ID: 0001_nodes
Revises:
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_nodes"
down_revision = None
branch_labels = None
depends_on = None

ROLE_VALUES = ("studio", "core")
REACHABILITY_VALUES = ("healthy", "degraded", "unreachable", "unknown")

# create_type=False on the column-level types: the enums are created explicitly below with
# checkfirst, and without this flag `create_table` would try to create them a second time and
# fail with DuplicateObjectError on any re-run.
NODE_ROLE = postgresql.ENUM(*ROLE_VALUES, name="node_role", create_type=False)
REACHABILITY = postgresql.ENUM(*REACHABILITY_VALUES, name="reachability", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*ROLE_VALUES, name="node_role").create(bind, checkfirst=True)
    postgresql.ENUM(*REACHABILITY_VALUES, name="reachability").create(bind, checkfirst=True)

    op.create_table(
        "nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("role", NODE_ROLE, nullable=False),
        sa.Column("mesh_address", postgresql.INET, nullable=False),
        sa.Column("egress_address", postgresql.INET, nullable=False),
        sa.Column("memory_total_bytes", sa.BigInteger, nullable=False),
        sa.Column("disk_total_bytes", sa.BigInteger, nullable=False),
        sa.Column("gpu_cores", sa.Integer, nullable=True),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reachability", REACHABILITY, nullable=False, server_default="unknown"),
        sa.Column("probe_failures", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_nodes_name", "nodes", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_nodes_name", table_name="nodes")
    op.drop_table("nodes")
    bind = op.get_bind()
    postgresql.ENUM(*REACHABILITY_VALUES, name="reachability").drop(bind, checkfirst=True)
    postgresql.ENUM(*ROLE_VALUES, name="node_role").drop(bind, checkfirst=True)
