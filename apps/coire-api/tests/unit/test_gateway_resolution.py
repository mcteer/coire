import uuid

import pytest
from pydantic import ValidationError

from coire_api.auth import ADMIN, Principal, PrincipalKind
from coire_api.db import ModelRow
from coire_api.gateway.resolution import _visible, retry_after_seconds
from coire_core.models.gateway import ChatCompletionRequest
from coire_core.models.registry import ModelState, Visibility


def model(**overrides: object) -> ModelRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "repo_id": "owner/repo",
        "slug": "owner--repo",
        "display_name": "Repo",
        "state": ModelState.READY,
        "visibility": Visibility.PUBLISHED,
        "entitlement": [],
        "tags": [],
        "placement_policy": "single:auto",
        "precision": "4bit",
        "weight_bytes": 1,
        "total_bytes": 1,
        "file_count": 1,
        "memory_estimate_bytes": 2,
        "capability_profile": {},
    }
    values.update(overrides)
    return ModelRow(**values)


def test_model_identifier_must_be_registry_uuid() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {"model": "../../caller/path", "messages": [{"role": "user", "content": "hi"}]}
        )


def test_model_identifier_remains_uuid() -> None:
    model_id = uuid.uuid4()
    request = ChatCompletionRequest.model_validate(
        {"model": str(model_id), "messages": [{"role": "user", "content": "hi"}]}
    )
    assert request.model == model_id


def test_unpublished_and_unentitled_are_indistinguishable_to_user() -> None:
    user = Principal(kind=PrincipalKind.USER, subject="user", scopes=frozenset({"general"}))
    assert not _visible(model(visibility=Visibility.ADMIN_ONLY), user)
    assert not _visible(model(entitlement=["explicit"]), user)
    assert _visible(model(visibility=Visibility.ADMIN_ONLY), ADMIN)


@pytest.mark.asyncio
async def test_retry_after_uses_rounded_up_measured_warmup() -> None:
    class Session:
        async def scalar(self, _statement: object) -> float:
            return 5.01

    assert await retry_after_seconds(Session(), uuid.uuid4(), fallback=30) == 6  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retry_after_falls_back_before_first_measurement() -> None:
    class Session:
        async def scalar(self, _statement: object) -> None:
            return None

    assert await retry_after_seconds(Session(), uuid.uuid4(), fallback=30) == 30  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_exact_smoke_resolution_uses_configured_isolated_node_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coire_api.gateway import resolution
    from coire_core.models.adapters import InferenceTarget
    from coire_core.settings import Settings

    model_id, variant_id, instance_id, engine_id = (uuid.uuid4() for _ in range(4))
    registry = model(id=model_id, backend="mlx_lm")
    identity = InferenceTarget(
        model_id=model_id, variant_id=variant_id, base_manifest_sha256="a" * 64
    )
    selected = SimpleNamespace(
        model=registry, variant=SimpleNamespace(id=variant_id), adapter=None, identity=identity
    )
    monkeypatch.setattr(resolution, "resolve_target", AsyncMock(return_value=selected))
    monkeypatch.setattr(resolution, "get_settings", lambda: Settings(node_listen_port=19470))
    session = AsyncMock()
    session.get.return_value = registry
    session.execute.side_effect = [
        SimpleNamespace(
            one_or_none=lambda: (
                SimpleNamespace(id=engine_id, rendering_identity=None),
                SimpleNamespace(name="coire-edge-a"),
                SimpleNamespace(path="/verified/private-model"),
                SimpleNamespace(id=instance_id, in_flight=0, created_at=datetime.now(UTC)),
            )
        ),
        SimpleNamespace(one_or_none=lambda: None),
    ]
    resolved = await resolution.resolve_exact_target(
        session,
        identity,
        ADMIN,
        instance_id=instance_id,
        upgrade_job_id="01M50N4E9MME5TZE3WJT96XMVV",
    )
    assert resolved.engine_url == f"http://coire-edge-a.lab:19470/node/engines/{engine_id}/proxy"
    assert resolved.instance_id == instance_id and resolved.target == identity


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["legacy", "sharded"])
async def test_other_registry_resolutions_use_configured_node_port(
    monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coire_api.gateway import resolution
    from coire_core.settings import Settings

    registry = model(backend="mlx_lm")
    engine_id, group_id, instance_id = (uuid.uuid4() for _ in range(3))
    selected = SimpleNamespace(model=registry, variant=None, adapter=None, identity=None)
    monkeypatch.setattr(resolution, "resolve_target", AsyncMock(return_value=selected))
    monkeypatch.setattr(resolution, "get_settings", lambda: Settings(node_listen_port=19470))
    node = SimpleNamespace(name="coire-edge-b")
    copy = SimpleNamespace(path="/verified/private-model")
    sharded = (
        SimpleNamespace(id=instance_id, in_flight=0, created_at=datetime.now(UTC)),
        SimpleNamespace(),
        node,
        copy,
        SimpleNamespace(id=group_id),
    )
    session = AsyncMock()
    session.get.return_value = registry
    session.execute.side_effect = [
        SimpleNamespace(one_or_none=lambda: None),
        SimpleNamespace(one_or_none=lambda: sharded if placement == "sharded" else None),
        SimpleNamespace(one_or_none=lambda: (SimpleNamespace(id=engine_id), node, copy)),
    ]
    resolved = await resolution.resolve_model(session, registry.id, ADMIN)
    path = f"shard-groups/{group_id}" if placement == "sharded" else f"engines/{engine_id}"
    assert resolved.engine_url == f"http://coire-edge-b.lab:19470/node/{path}/proxy"
