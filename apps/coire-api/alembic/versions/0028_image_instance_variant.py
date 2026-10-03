"""Allow image worker instances to bind a base model without a language variant.

Revision ID: 0028_image_instance_variant
Revises: 0027_image_capability_profile
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_image_instance_variant"
down_revision: str | None = "0027_image_capability_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("model_instances", "variant_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        "ck_model_instance_variant_policy",
        "model_instances",
        "variant_id IS NOT NULL OR policy LIKE 'image:%'",
    )


def downgrade() -> None:
    occupied = op.get_bind().scalar(
        sa.text("SELECT 1 FROM model_instances WHERE variant_id IS NULL LIMIT 1")
    )
    if occupied is not None:
        raise RuntimeError("stop image workers before downgrading instance variants")
    op.drop_constraint("ck_model_instance_variant_policy", "model_instances", type_="check")
    op.alter_column("model_instances", "variant_id", existing_type=sa.Uuid(), nullable=False)
