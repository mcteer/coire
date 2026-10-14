"""A Core maintenance exemption permits only fixed generation on its exact tiny engine."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState, EngineStatus
from coire_core.models.registry import EngineBackend
from coire_core.models.upgrades import CoreNodeSmokeScope
from coire_core.settings import Settings
from coire_node import core_smoke
from coire_node.engines import EngineManager
from coire_node.maintenance import NodeMaintenance
from coire_node.store import Store


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "path",
        "node",
        "runtime",
        "expired",
        "fence",
        "tokens",
        "prompt",
        "engine",
        "variant",
        "manifest",
    ],
)
def test_core_smoke_cannot_widen_fenced_engine_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = core_plan()
    settings = Settings(upgrades_enabled=True, node_name="coire-edge-a")
    gate = NodeMaintenance(tmp_path / "fence", "coire-edge-a")
    gate.store.write("maintenance.json", plan.maintenance)
    scope = CoreNodeSmokeScope(
        plan=plan,
        node="coire-edge-b" if fault == "node" else "coire-edge-a",
        engine_id=uuid4(),
        runtime_sha256="a" * 64,
        deadline_at=datetime.now(UTC) + timedelta(seconds=-1 if fault == "expired" else 60),
    )
    body = core_smoke.fixed_smoke_request(scope)
    if fault == "tokens":
        body = body.model_copy(update={"max_tokens": 65})
    if fault == "prompt":
        body.messages[0] = body.messages[0].model_copy(update={"content": "arbitrary user work"})
    if fault == "fence":
        gate.store.write(
            "maintenance.json",
            plan.maintenance.model_copy(update={"job_id": "01M6MHS0000000000000000009"}),
        )
    engine = EngineStatus(
        engine_id=scope.engine_id,
        slug=plan.model_slug,
        backend=EngineBackend.MLX_LM,
        target=InferenceTarget(
            model_id=plan.model_id,
            variant_id=uuid4() if fault == "variant" else plan.model_variant_id,
            base_manifest_sha256=plan.model_manifest_sha256,
        ),
        port=39210,
        started_at=datetime.now(UTC),
        state=EngineState.FAILED if fault == "engine" else EngineState.READY,
    )
    engines = cast(EngineManager, SimpleNamespace(get=Mock(return_value=engine)))
    store = cast(
        Store,
        SimpleNamespace(
            read_manifest=Mock(
                return_value=SimpleNamespace(
                    total_bytes=1024,
                    sha256=lambda: "f" * 64 if fault == "manifest" else plan.model_manifest_sha256,
                )
            )
        ),
    )
    monkeypatch.setattr(
        core_smoke,
        "running_runtime_sha256",
        lambda _settings: "b" * 64 if fault == "runtime" else "a" * 64,
    )
    path = f"/node/engines/{scope.engine_id}/proxy/v1/chat/completions"
    if fault == "path":
        path = "/node/engines"
    if fault is None:
        with gate.transfer_continuation(
            lambda: core_smoke.authorize_core_smoke(body, path, settings, gate, engines, store)
        ):
            assert gate._admitting == 1
        assert gate._admitting == 0
        assert gate.blocked()
    else:
        with (
            pytest.raises(UpgradeConflict),
            gate.transfer_continuation(
                lambda: core_smoke.authorize_core_smoke(body, path, settings, gate, engines, store)
            ),
        ):
            pytest.fail("invalid Core smoke scope acquired node admission")
        assert gate._admitting == 0
