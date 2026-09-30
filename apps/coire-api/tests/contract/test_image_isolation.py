"""Image and auxiliary assets cannot cross into language/vision routing."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ModelRow
from coire_api.gateway.resolution import ModelNotFoundError, resolve_model
from coire_api.registry import service
from coire_core.models.registry import (
    EngineBackend,
    ModelAddRequest,
    ModelKind,
    ModelState,
    Visibility,
)


def _model(backend: EngineBackend) -> ModelRow:
    return ModelRow(
        id=uuid.uuid4(),
        repo_id="owner/image",
        slug="owner--image",
        display_name="Image",
        backend=backend,
        source="studio",
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        entitlement=[],
    )


@pytest.mark.parametrize("backend", [EngineBackend.MFLUX, EngineBackend.AUXILIARY])
async def test_image_backend_never_appears_in_chat_or_direct_resolution(
    backend: EngineBackend,
) -> None:
    row = _model(backend)
    admin = Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4())
    user = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    assert not service.visible_to(is_admin=True, model=row)
    assert not service.visible_to(is_admin=False, model=row)
    assert not service.chat_model_eligible(row, admin)
    assert not service.chat_model_eligible(row, user)

    class RowSession:
        async def get(self, model: type[object], identity: object) -> ModelRow:
            assert model is ModelRow and identity == row.id
            return row

        async def execute(self, query: object) -> None:
            raise AssertionError("image backend must not reach engine selection")

    with pytest.raises(ModelNotFoundError):
        await resolve_model(cast(AsyncSession, RowSession()), row.id, admin)


def test_language_and_vision_backends_remain_chat_eligible() -> None:
    for backend in (EngineBackend.MLX_LM, EngineBackend.MLX_VLM):
        row = _model(backend)
        assert service.visible_to(is_admin=True, model=row)
        assert service.visible_to(is_admin=False, model=row)


@pytest.mark.parametrize(
    "kind",
    [
        ModelKind.IMAGE_MODEL,
        ModelKind.IMAGE_LORA,
        ModelKind.CONTROL_MODEL,
        ModelKind.UPSCALE_MODEL,
        ModelKind.IMAGE_CLASSIFIER,
    ],
)
def test_image_kind_never_routes_even_with_malformed_language_backend(kind: ModelKind) -> None:
    row = _model(EngineBackend.MLX_LM)
    row.kind = kind
    assert not service.is_chat_backend(row)
    assert not service.visible_to(is_admin=True, model=row)
    assert not service.visible_to(is_admin=False, model=row)


@pytest.mark.parametrize(
    "kind",
    [
        ModelKind.IMAGE_MODEL,
        ModelKind.IMAGE_LORA,
        ModelKind.CONTROL_MODEL,
        ModelKind.UPSCALE_MODEL,
        ModelKind.IMAGE_CLASSIFIER,
    ],
)
async def test_legacy_add_refuses_image_kind_before_inspection(
    kind: ModelKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    audits: list[dict[str, Any]] = []

    async def capture_audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(service, "write_audit", capture_audit)

    class NoInspectionSession:
        async def execute(self, query: object) -> None:
            raise AssertionError("rejected kind must not inspect or acquire")

    with pytest.raises(service.RegistryError) as exc:
        await service.add_model(
            cast(AsyncSession, NoInspectionSession()),
            ModelAddRequest(repo_id="owner/image", kind=kind),
            client=cast(Any, object()),
            settings=cast(Any, object()),
            views=[],
            actor="admin:test",
        )
    assert exc.value.status_code == 422
    assert len(audits) == 1
    assert audits[0]["target_id"] == "owner/image"
