"""Add separated control/data endpoint identities to nodes.

Revision ID: 0003_node_endpoints
Revises: 0002_registry
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_node_endpoints"
down_revision = "0002_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable by design: legacy agents continue to populate only mesh/egress fields during
    # the mixed-version rollout. Old addresses are not reinterpreted as control endpoints.
    op.add_column("nodes", sa.Column("endpoint_contract_version", sa.Integer(), nullable=True))
    op.add_column("nodes", sa.Column("control_host", sa.String(length=255), nullable=True))
    op.add_column("nodes", sa.Column("data_host", sa.String(length=255), nullable=True))
    op.alter_column("nodes", "mesh_address", nullable=True)


def downgrade() -> None:
    op.alter_column("nodes", "mesh_address", nullable=False)
    op.drop_column("nodes", "data_host")
    op.drop_column("nodes", "control_host")
    op.drop_column("nodes", "endpoint_contract_version")
