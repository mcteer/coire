"""Merge gateway usage and separated node endpoint histories.

Revision ID: 0004_merge_gateway_fabrics
Revises: 0003_gateway_usage, 0003_node_endpoints
"""

from __future__ import annotations

revision = "0004_merge_gateway_fabrics"
down_revision = ("0003_gateway_usage", "0003_node_endpoints")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
