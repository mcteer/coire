"""The gateway exemption rechecks immutable authority and actual Studio ownership."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import EngineProcessRow, ModelInstanceRow, NodeRow
from coire_api.upgrades import gateway_smoke_scope as service
from coire_core.errors import UpgradeConflict
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.upgrades import CoreNodeSmokeScope, UpgradePhase
from coire_core.settings import Settings


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("loading", [False, True])
@pytest.mark.parametrize(
    "fault", [None, "phase", "cancel", "engine", "instance", "node", "runtime", "asset", "fence"]
)
async def test_core_smoke_admission_is_bound_to_live_operation_and_exact_instance(
    monkeypatch: pytest.MonkeyPatch, restored: bool, loading: bool, fault: str | None
) -> None:
    plan = core_plan()
    node_id, instance_id, engine_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    scope = CoreNodeSmokeScope(
        plan=plan,
        node="coire-edge-a",
        engine_id=engine_id,
        runtime_sha256="a" * 64,
        deadline_at=now + timedelta(seconds=60),
    )
    operation = plan.operation.model_copy(
        update={
            "phase": UpgradePhase.VERIFYING_SOURCE if restored else UpgradePhase.VERIFYING,
            "cancel_requested": fault == "cancel",
        }
    )
    if fault == "phase":
        operation = plan.operation
    row = SimpleNamespace(
        payload=operation.model_dump(mode="json"),
        user_id=plan.operation.user_id,
        key_id=None,
        credential_version=None,
    )
    engine = SimpleNamespace(
        node_id=node_id,
        instance_id=instance_id,
        model_id=plan.model_id,
        variant_id=plan.model_variant_id,
        adapter_id=None,
        backend="mlx_lm",
        state=EngineState.STOPPED if fault == "engine" else EngineState.READY,
    )
    instance = SimpleNamespace(
        model_id=plan.model_id,
        variant_id=plan.model_variant_id,
        adapter_id=None,
        state=InstanceState.DRAINING
        if fault == "instance"
        else InstanceState.REQUESTED
        if loading
        else InstanceState.READY,
        upgrade_smoke_payload=scope.model_dump(mode="json"),
    )
    node = SimpleNamespace(
        id=node_id,
        name="coire-edge-b" if fault == "node" else "coire-edge-a",
        runtime_sha256="b" * 64 if fault == "runtime" else "a" * 64,
        health_observed_at=now,
    )
    fence = SimpleNamespace(
        job_id="01M6MHS0000000000000000009" if fault == "fence" else plan.operation.job_id,
        generation=plan.operation.generation,
        payload=plan.maintenance.model_dump(mode="json"),
    )

    async def get(model: type[object], _identity: object, **_kwargs: object) -> object:
        if model is EngineProcessRow:
            return engine
        if model is ModelInstanceRow:
            return instance
        assert model is NodeRow
        return node

    session = AsyncMock()
    session.get.side_effect = get
    session.scalars.return_value = SimpleNamespace(all=lambda: [fence])
    monkeypatch.setattr(service, "scoped_operation", AsyncMock(return_value=(row, plan)))
    authorized = AsyncMock(return_value=plan.operation.user_id)
    monkeypatch.setattr(service, "authorize", authorized)
    asset = (
        plan.model_id,
        plan.model_variant_id,
        plan.model_slug,
        "f" * 64 if fault == "asset" else plan.model_manifest_sha256,
    )
    monkeypatch.setattr(service, "smoke_asset", AsyncMock(return_value=asset))
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject="coire-upgrade-executor",
        scopes=frozenset({"upgrades:execute"}),
    )
    with service.gateway_smoke_scope(
        scope,
        principal,
        Settings(upgrades_enabled=True),
        instance_id,
        restored=restored,
        loading=loading,
    ):
        if fault is None or (fault == "cancel" and restored):
            assert await service.smoke_admission_exemption(session, [node_id]) is True
        else:
            with pytest.raises(UpgradeConflict):
                await service.smoke_admission_exemption(session, [node_id])
    assert await service.smoke_admission_exemption(session, [node_id]) is None
    if restored:
        authorized.assert_not_awaited()
