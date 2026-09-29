"""Persist healthy Chat Stop as a distinct usage outcome.

Revision ID: 0022_stopped_usage_outcome
Revises: 0021_provider_model_targets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_stopped_usage_outcome"
down_revision: str | None = "0021_provider_model_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE usage_outcome ADD VALUE IF NOT EXISTS 'stopped'")


def downgrade() -> None:
    bind = op.get_bind()
    stopped = bind.scalar(sa.text("SELECT 1 FROM usage_records WHERE outcome = 'stopped' LIMIT 1"))
    if stopped is not None:
        raise RuntimeError("export or remove stopped usage records before downgrading")
    op.execute("ALTER TYPE usage_outcome RENAME TO usage_outcome_with_stopped")
    op.execute(
        "CREATE TYPE usage_outcome AS ENUM ('succeeded', 'failed', 'disconnected', 'refused')"
    )
    op.execute(
        "ALTER TABLE usage_records ALTER COLUMN outcome TYPE usage_outcome "
        "USING outcome::text::usage_outcome"
    )
    op.execute("DROP TYPE usage_outcome_with_stopped")
