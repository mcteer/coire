"""Index live published outputs for bounded retention passes.

Revision ID: 0030_image_output_retention
Revises: 0029_image_acquisition_source
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_image_output_retention"
down_revision: str | None = "0029_image_acquisition_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_image_outputs_retention",
        "image_outputs",
        ["published_at", "id"],
        postgresql_where=sa.text("state = 'published' AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_image_outputs_retention", table_name="image_outputs")
