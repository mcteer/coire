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
    # Deployments that had already advanced along the acquisition branch before this
    # historical revision was linked in may not have these columns. Their rollback must
    # still reach the common parent without failing on an absent optional observation.
    present = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("nodes")}
    for name in (
        "heartbeat_latency_ms",
        "last_observed_at",
        "last_observation",
        "probe_degraded",
        "probe_successes",
    ):
        if name in present:
            op.drop_column("nodes", name)
