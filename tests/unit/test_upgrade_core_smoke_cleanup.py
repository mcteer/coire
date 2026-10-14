"""Revocation is durable before signaling, and uncertainty never releases ownership."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineStartRequest, EngineState, EngineStatus
from coire_core.models.upgrades import CoreNodeSmokeLifecycle, CoreNodeSmokeScope
from coire_core.settings import Settings
from coire_node import core_smoke
from coire_node.engines import EngineManager
from coire_node.maintenance import NodeMaintenance
from coire_node.store import Store
from coire_node.upgrade_inventory import core_smoke_entries


@pytest.mark.parametrize("fault", [None, "pending", "foreign", "missing"])
def test_owned_core_smoke_cleanup_cannot_stop_another_target_or_reopen_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = core_plan()
    settings = Settings(
        upgrades_enabled=True, node_name="coire-edge-a", node_state_dir=str(tmp_path / "state")
    )
    gate = NodeMaintenance(tmp_path / "fence", "coire-edge-a")
    gate.store.write("maintenance.json", plan.maintenance)
    scope = CoreNodeSmokeScope(
        plan=plan,
        node="coire-edge-a",
        engine_id=uuid4(),
        runtime_sha256="a" * 64,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    private = core_smoke.core_smoke_store(settings, scope)
    if fault != "missing":
        private.write(
            "lifecycle.json", CoreNodeSmokeLifecycle(scope=scope, updated_at=datetime.now(UTC))
        )
        assert core_smoke_entries(Path(settings.node_state_dir))[0].state == "active"
    target = InferenceTarget(
        model_id=plan.model_id,
        variant_id=uuid4() if fault == "foreign" else plan.model_variant_id,
        base_manifest_sha256=plan.model_manifest_sha256,
    )
    status = EngineStatus(
        engine_id=scope.engine_id,
        slug=plan.model_slug,
        target=target,
        port=39210,
        state=EngineState.READY,
        started_at=datetime.now(UTC),
    )
    observed = [status]

    def stop(_identity: object) -> EngineStatus:
        retained = private.read("lifecycle.json", CoreNodeSmokeLifecycle)
        assert retained is not None and retained.state == "revoked"
        observed[0] = status.model_copy(update={"state": EngineState.STOPPING})
        return observed[0]

    stopped = Mock(side_effect=stop)
    manager = cast(
        EngineManager,
        SimpleNamespace(_lock=gate.lock, get=lambda _identity: observed[0], stop=stopped),
    )
    monkeypatch.setattr(core_smoke, "running_runtime_sha256", lambda _settings: "a" * 64)
    if fault in {"foreign", "missing"}:
        with pytest.raises(UpgradeConflict):
            core_smoke.cleanup_core_smoke(settings, scope, gate, manager)
        stopped.assert_not_called()
        return
    with pytest.raises(UpgradeUnavailable):
        core_smoke.cleanup_core_smoke(settings, scope, gate, manager)
    assert core_smoke_entries(Path(settings.node_state_dir))[0].state == "cleaning"
    if fault == "pending":
        with pytest.raises(UpgradeUnavailable):
            core_smoke.cleanup_core_smoke(settings, scope, gate, manager)
        stopped.assert_called_once()
        return
    observed[0] = status.model_copy(update={"state": EngineState.STOPPED})
    receipt = core_smoke.cleanup_core_smoke(settings, scope, gate, manager)
    assert receipt.engine_id == scope.engine_id and receipt.scope_sha256 == scope.sha256()
    assert not core_smoke_entries(Path(settings.node_state_dir))
    store = cast(
        Store,
        SimpleNamespace(
            read_manifest=lambda _slug: SimpleNamespace(
                total_bytes=1024,
                sha256=lambda: plan.model_manifest_sha256,
            )
        ),
    )
    body = EngineStartRequest(
        engine_id=scope.engine_id,
        slug=plan.model_slug,
        target=InferenceTarget(
            model_id=plan.model_id,
            variant_id=plan.model_variant_id,
            base_manifest_sha256=plan.model_manifest_sha256,
        ),
        estimate_bytes=1024**3,
        coire_upgrade_scope=scope,
    )
    # Even an absent old engine does not allow a delayed load after revocation.
    absent = cast(EngineManager, SimpleNamespace(get=lambda _identity: None))
    with (
        pytest.raises(UpgradeConflict),
        core_smoke.core_smoke_admission(body, "/node/engines", settings, gate, absent, store),
    ):
        pytest.fail("released Core smoke lifecycle resumed a delayed start")
