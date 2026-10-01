"""Ready image bases require a dedicated measured capability profile."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint

from coire_api.db import Base


def test_registry_has_dedicated_image_profile_constraint() -> None:
    table = Base.metadata.tables["models"]
    assert "image_capability_profile" in table.c
    assert any(
        isinstance(item, CheckConstraint) and "image_capability_profile" in str(item.sqltext)
        for item in table.constraints
    )


def test_image_profile_migration_refuses_lossy_downgrade() -> None:
    path = Path("apps/coire-api/alembic/versions/0027_image_capability_profile.py")
    namespace = runpy.run_path(str(path))
    assert namespace["down_revision"] == "0026_image_registry_kind"

    class OccupiedBind:
        def scalar(self, statement: object) -> int:
            return 1

    class OccupiedOp:
        def get_bind(self) -> OccupiedBind:
            return OccupiedBind()

    namespace["downgrade"].__globals__["op"] = OccupiedOp()
    with pytest.raises(RuntimeError, match="image capability"):
        namespace["downgrade"]()
