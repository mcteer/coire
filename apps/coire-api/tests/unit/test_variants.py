from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from coire_api.db import ModelRow, ModelVariantRow
from coire_api.registry.acquisition import AcquisitionError
from coire_api.registry.variants import update_publication
from coire_core.models.acquisition import Precision, VariantPublication, VariantRecipe, VariantState
from coire_core.models.registry import ModelState, Visibility


class _Result:
    def first(self) -> None:
        return None


class _Session:
    def __init__(self, model: ModelRow) -> None:
        self.model = model

    async def execute(self, _statement: object) -> _Result:
        return _Result()

    async def get(self, model_type: type[object], _row_id: uuid.UUID) -> object | None:
        return self.model if model_type is ModelRow else None


def _rows(*, validated: bool, state: VariantState) -> tuple[ModelRow, ModelVariantRow]:
    now = datetime.now(UTC)
    model_id = uuid.uuid4()
    model = ModelRow(
        id=model_id,
        slug="org--model",
        repo_id="org/model",
        display_name="Model",
        state=ModelState.READY,
        visibility=Visibility.ADMIN_ONLY,
        precision="bf16",
        weight_bytes=1,
        total_bytes=1,
        file_count=1,
        memory_estimate_bytes=1,
        created_at=now,
        updated_at=now,
    )
    variant = ModelVariantRow(
        id=uuid.uuid4(),
        model_id=model_id,
        name="4bit",
        slug="org--model.4bit",
        source_revision="a" * 40,
        precision=Precision.BIT4.value,
        recipe=VariantRecipe(name="4bit", precision=Precision.BIT4).model_dump(mode="json"),
        state=state,
        byte_size=1,
        memory_estimate_bytes=1,
        validated=validated,
        published=False,
        is_default=False,
        raw_retained=False,
        created_at=now,
        updated_at=now,
    )
    return model, variant


@pytest.mark.asyncio
async def test_unvalidated_variant_cannot_be_published(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model, variant = _rows(validated=False, state=VariantState.VALIDATING)

    async def _audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("coire_api.registry.variants.write_audit", _audit)
    with pytest.raises(AcquisitionError, match="ready validated"):
        await update_publication(
            _Session(model),  # type: ignore[arg-type]
            variant,
            VariantPublication(published=True),
            actor="admin",
        )


@pytest.mark.asyncio
async def test_selecting_default_also_publishes_and_projects_legacy_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model, variant = _rows(validated=True, state=VariantState.READY)

    async def _audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("coire_api.registry.variants.write_audit", _audit)
    projected = await update_publication(
        _Session(model),  # type: ignore[arg-type]
        variant,
        VariantPublication(is_default=True),
        actor="admin",
    )

    assert projected.published is True
    assert projected.is_default is True
    assert model.visibility is Visibility.PUBLISHED
    assert model.precision == Precision.BIT4.value
