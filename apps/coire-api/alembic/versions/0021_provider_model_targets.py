"""Registry targets for admin-curated external text models.

Revision ID: 0021_provider_model_targets
Revises: 0020_chat_activity_sequence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_provider_model_targets"
down_revision: str | None = "0020_chat_activity_sequence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "models", sa.Column("source", sa.String(16), nullable=False, server_default="studio")
    )
    op.add_column("models", sa.Column("provider_model_id", sa.String(120), nullable=True))
    op.add_column("models", sa.Column("max_output_tokens", sa.Integer(), nullable=True))
    op.add_column("models", sa.Column("daily_token_budget", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_models_provider_target",
        "models",
        "(source = 'studio' AND provider_model_id IS NULL AND max_output_tokens IS NULL "
        "AND daily_token_budget IS NULL) OR "
        "(source IN ('openai', 'anthropic') AND provider_model_id IS NOT NULL "
        "AND max_output_tokens > 0 AND daily_token_budget > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_models_provider_target", "models", type_="check")
    op.drop_column("models", "daily_token_budget")
    op.drop_column("models", "max_output_tokens")
    op.drop_column("models", "provider_model_id")
    op.drop_column("models", "source")
