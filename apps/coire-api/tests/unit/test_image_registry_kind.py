"""Persisted asset kind must agree with backend and never enter chat resolution."""

from __future__ import annotations

import runpy
import uuid
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint

from coire_api.db import Base, ModelRow
from coire_api.registry.service import is_chat_backend
from coire_core.models.registry import EngineBackend, ModelKind


def test_registry_kind_is_stored_and_chat_requires_language_kind() -> None:
    table = Base.metadata.tables["models"]
    assert "kind" in table.c
    assert any(
        isinstance(item, CheckConstraint) and "image_model" in str(item.sqltext)
        for item in table.constraints
    )
    row = ModelRow(id=uuid.uuid4(), kind=ModelKind.IMAGE_MODEL, backend=EngineBackend.MLX_LM)
    assert not is_chat_backend(row)
    row.kind = ModelKind.LANGUAGE_MODEL
    assert is_chat_backend(row)


def test_registry_kind_migration_is_guarded_and_reversible() -> None:
    path = Path("apps/coire-api/alembic/versions/0026_image_registry_kind.py")
    namespace = runpy.run_path(str(path))
    assert namespace["down_revision"] == "0025_image_capacity"

    class OccupiedBind:
        def scalar(self, statement: object) -> int:
            return 1

    class OccupiedOp:
        def get_bind(self) -> OccupiedBind:
            return OccupiedBind()

    namespace["downgrade"].__globals__["op"] = OccupiedOp()
    with pytest.raises(RuntimeError, match="image assets"):
        namespace["downgrade"]()
