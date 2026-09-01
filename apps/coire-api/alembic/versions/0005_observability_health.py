"""Persist damped, fresh node health observations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_observability_health"
down_revision = "0004_merge_gateway_fabrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes", sa.Column("probe_successes", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "nodes", sa.Column("probe_degraded", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("nodes", sa.Column("last_observation", postgresql.JSONB(), nullable=True))
    op.add_column("nodes", sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("nodes", sa.Column("heartbeat_latency_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "heartbeat_latency_ms")
    op.drop_column("nodes", "last_observed_at")
    op.drop_column("nodes", "last_observation")
    op.drop_column("nodes", "probe_degraded")
    op.drop_column("nodes", "probe_successes")
