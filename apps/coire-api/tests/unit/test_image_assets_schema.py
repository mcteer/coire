"""Asset relationships that must hold even when service transactions fail."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from coire_api.db import Base


def _composite_targets(table_name: str) -> set[frozenset[str]]:
    table = Base.metadata.tables[table_name]
    return {
        frozenset(fk.target_fullname for fk in constraint.elements)
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_output_and_grant_owner_cannot_disagree_with_parent() -> None:
    assert frozenset({"image_jobs.id", "image_jobs.owner_user_id"}) in _composite_targets(
        "image_outputs"
    )
    assert frozenset({"image_outputs.id", "image_outputs.owner_user_id"}) in _composite_targets(
        "image_download_grants"
    )
    output = Base.metadata.tables["image_outputs"]
    assert any(
        isinstance(c, UniqueConstraint)
        and {x.name for x in c.columns} == {"job_id", "output_index"}
        for c in output.constraints
    )


def test_recipe_input_and_transfer_slot_constraints() -> None:
    inputs = Base.metadata.tables["image_inputs"]
    assert any(
        isinstance(c, CheckConstraint)
        and "purpose = 'recipe'" in str(c.sqltext)
        and "normalized_key IS NULL" in str(c.sqltext)
        and "recipe IS NOT NULL" in str(c.sqltext)
        for c in inputs.constraints
    )
    assert any(
        isinstance(c, CheckConstraint)
        and "state <> 'ready'" in str(c.sqltext)
        and "original_sha256 IS NOT NULL" in str(c.sqltext)
        for c in inputs.constraints
    )
    transfers = Base.metadata.tables["image_transfers"]
    assert {"job_id", "attempt", "output_index"} == {c.name for c in transfers.primary_key.columns}
    grants = Base.metadata.tables["image_download_grants"]
    assert "grant_hash" in grants.primary_key.columns


def test_asset_migration_has_guarded_reverse() -> None:
    path = Path("apps/coire-api/alembic/versions/0024_image_assets.py")
    migration = runpy.run_path(str(path))
    assert migration["down_revision"] == "0023_image_jobs_presets"

    class OccupiedBind:
        def scalar(self, statement: object) -> int:
            return 1

    class OccupiedOp:
        def get_bind(self) -> OccupiedBind:
            return OccupiedBind()

    migration["downgrade"].__globals__["op"] = OccupiedOp()
    with pytest.raises(RuntimeError, match="image asset records"):
        migration["downgrade"]()
