"""Regeneration resolves immutable registry targets rather than current defaults."""

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.gateway import resolution
from coire_core.models.adapters import InferenceTarget


def exact_target(*, adapter: bool = False) -> InferenceTarget:
    return InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        base_manifest_sha256="a" * 64,
        adapter_id=uuid.uuid4() if adapter else None,
        adapter_manifest_sha256="b" * 64 if adapter else None,
    )


@pytest.mark.parametrize("adapter", [False, True])
async def test_exact_resolve_uses_registry_variant_and_selector(
    monkeypatch: pytest.MonkeyPatch,
    adapter: bool,
) -> None:
    target = exact_target(adapter=adapter)
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    result = resolution.ResolvedModel(
        target.model_id, "safe", 4096, "/safe", uuid.uuid4(), "studio", "http://node", target=target
    )
    resolver = AsyncMock(return_value=result)
    monkeypatch.setattr(resolution, "resolve_model", resolver)
    row = SimpleNamespace(
        selector=f"{target.model_id}@parent",
        model_id=target.model_id,
        base_variant_id=target.variant_id,
    )
    session = cast(AsyncSession, SimpleNamespace(get=AsyncMock(return_value=row)))
    assert await resolution.resolve_exact_target(session, target, principal) == result
    resolver.assert_awaited_once_with(
        session,
        row.selector if adapter else target.model_id,
        principal,
        variant_id=target.variant_id,
    )
    resolver.return_value = replace(
        result, target=target.model_copy(update={"base_manifest_sha256": "c" * 64})
    )
    with pytest.raises(resolution.ModelNotFoundError):
        await resolution.resolve_exact_target(session, target, principal)


async def test_exact_adapter_missing_never_falls_back_to_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = exact_target(adapter=True)
    resolver = AsyncMock()
    monkeypatch.setattr(resolution, "resolve_model", resolver)
    session = cast(AsyncSession, SimpleNamespace(get=AsyncMock(return_value=None)))
    with pytest.raises(resolution.ModelNotFoundError):
        await resolution.resolve_exact_target(session, target, Principal())
    resolver.assert_not_awaited()
