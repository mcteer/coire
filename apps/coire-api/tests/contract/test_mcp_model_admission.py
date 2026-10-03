"""Read-only MCP admission must not inherit the write verification gate."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ModelRow, ModelVariantRow
from coire_api.runs import variant_gate
from coire_core.models.harness import TaskClass
from coire_core.models.registry import EngineBackend, ModelKind, ModelState, Visibility
from coire_mcp import tools


def _compiled_gate(task_class: TaskClass) -> str:
    query = select(ModelVariantRow.id).where(*variant_gate(task_class))
    return str(query)


def test_read_variant_gate_accepts_published_unverified_variant() -> None:
    query = _compiled_gate(TaskClass.READ)
    assert "model_variants.validated" in query
    assert "model_variants.published" in query
    assert "model_variants.harness_verified" not in query


def test_write_variant_gate_requires_harness_verification() -> None:
    query = _compiled_gate(TaskClass.WRITE)
    assert "model_variants.validated" in query
    assert "model_variants.published" in query
    assert "model_variants.harness_verified" in query


async def test_mcp_model_selection_refuses_unentitled_requested_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ModelRow(
        id=uuid.uuid4(),
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        tags=["coding"],
        entitlement=["paid"],
    )

    class Rows:
        def all(self) -> list[ModelRow]:
            return [model]

    class Session:
        async def scalars(self, _query: object) -> Rows:
            return Rows()

        async def scalar(self, _query: object) -> uuid.UUID:
            return uuid.uuid4()

    @asynccontextmanager
    async def session_scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(tools, "session_scope", session_scope)
    principal = Principal(
        kind=PrincipalKind.API_KEY,
        user_id=uuid.uuid4(),
        scopes=frozenset({"mcp"}),
    )
    with pytest.raises(ValueError, match="no entitled"):
        await tools._choose_model(principal, model.id, TaskClass.READ)


@pytest.mark.parametrize(
    ("kind", "backend"),
    [
        (ModelKind.IMAGE_MODEL, EngineBackend.MFLUX),
        (ModelKind.IMAGE_LORA, EngineBackend.AUXILIARY),
        (ModelKind.IMAGE_MODEL, EngineBackend.MLX_LM),
    ],
)
async def test_mcp_model_selection_excludes_image_assets_even_with_coding_tag(
    monkeypatch: pytest.MonkeyPatch, kind: ModelKind, backend: EngineBackend
) -> None:
    model = ModelRow(
        id=uuid.uuid4(),
        kind=kind,
        backend=backend,
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        tags=["coding"],
        entitlement=[],
    )

    class Rows:
        def all(self) -> list[ModelRow]:
            return [model]

    class Session:
        async def scalars(self, _query: object) -> Rows:
            return Rows()

        async def scalar(self, _query: object) -> uuid.UUID:
            return uuid.uuid4()

    @asynccontextmanager
    async def session_scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(tools, "session_scope", session_scope)
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    with pytest.raises(ValueError, match="no entitled"):
        await tools._choose_model(principal, model.id, TaskClass.READ)
