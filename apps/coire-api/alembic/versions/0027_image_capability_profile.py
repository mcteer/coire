"""Persist measured image base capability separately from chat capability.

Revision ID: 0027_image_capability_profile
Revises: 0026_image_registry_kind
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_image_capability_profile"
down_revision: str | None = "0026_image_registry_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column("image_capability_profile", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "ck_models_image_capability",
        "models",
        "(image_capability_profile IS NULL OR kind = 'image_model') AND "
        "(kind <> 'image_model' OR state <> 'ready' OR image_capability_profile IS NOT NULL)",
    )


def downgrade() -> None:
    occupied = op.get_bind().scalar(
        sa.text("SELECT 1 FROM models WHERE image_capability_profile IS NOT NULL LIMIT 1")
    )
    if occupied is not None:
        raise RuntimeError("remove image capability profiles before downgrading")
    op.drop_constraint("ck_models_image_capability", "models", type_="check")
    op.drop_column("models", "image_capability_profile")
