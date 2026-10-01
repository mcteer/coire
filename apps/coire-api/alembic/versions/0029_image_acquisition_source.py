"""Persist pinned image source and the admin-reviewed model licence.

Revision ID: 0029_image_acquisition_source
Revises: 0028_image_instance_variant
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0029_image_acquisition_source"
down_revision: str | None = "0028_image_instance_variant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("models", sa.Column("source_revision", sa.String(40), nullable=True))
    op.add_column("models", sa.Column("license_id", sa.String(120), nullable=True))
    op.add_column(
        "models", sa.Column("image_validated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("download_jobs", sa.Column("expected_files", JSONB(), nullable=True))
    op.add_column("download_jobs", sa.Column("image_validation", JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_models_image_acquisition_provenance",
        "models",
        "(kind = 'language_model') OR (source_revision IS NULL AND license_id IS NULL) OR "
        "(source_revision ~ '^[0-9a-f]{40}$' AND source_revision <> repeat('0', 40) "
        "AND length(license_id) > 0)",
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM models WHERE kind <> 'language_model' "
            "AND (source_revision IS NOT NULL OR license_id IS NOT NULL)"
        )
    )
    if count:
        raise RuntimeError("remove acquired image provenance before downgrading")
    op.drop_constraint("ck_models_image_acquisition_provenance", "models", type_="check")
    op.drop_column("download_jobs", "image_validation")
    op.drop_column("download_jobs", "expected_files")
    op.drop_column("models", "image_validated_at")
    op.drop_column("models", "license_id")
    op.drop_column("models", "source_revision")
