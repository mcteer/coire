"""Node upgrade transport is authenticated, strict and unavailable before bootstrap."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from upgrade_fixtures import plan

from coire_core.models.node import NetworkPath
from coire_core.settings import Settings
from coire_node.agent import create_app
from coire_node.maintenance import NodeMaintenance
from coire_node.store import Store
from coire_node.testing.harness import TOKEN, StubCollector

AUTH = {"Authorization": "Bearer " + TOKEN}


def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        node_control_host="127.0.0.1",
        node_name="coire-edge-a",
        upgrades_enabled=True,
        node_token=SecretStr(TOKEN),
        legacy_network_mode=False,
        node_state_dir=str(tmp_path / "state"),
        upgrade_bootstrap_root=str(tmp_path / "bootstrap"),
    )
    maintenance = NodeMaintenance(tmp_path / "state/upgrades", "coire-edge-a")
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(tmp_path / "models"),
        maintenance=maintenance,
    )
    return TestClient(app)


def cleanup_scope():
    from datetime import UTC, datetime, timedelta
    from uuid import NAMESPACE_URL, uuid5

    from coire_core.models.upgrades import (
        NodeUpgradeCollectiveRead,
        NodeUpgradeCollectiveScope,
    )

    value = plan(paired=True)
    read = NodeUpgradeCollectiveRead(
        generation=value.operation.generation,
        plan_sha256=value.authority_sha256(),
        runtime_sha256=value.target.manifest.environment_sha256,
        stage="candidate",
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    scope = NodeUpgradeCollectiveScope(
        plan=value,
        request=read,
        index=0,
        command_id=uuid5(
            NAMESPACE_URL,
            f"coire-upgrade-collective/{value.operation.job_id}/candidate/{read.plan_sha256}/0",
        ),
    )
    return scope


def test_cleanup_route_authentication_and_path_refusal_have_no_effects(tmp_path: Path) -> None:
    from coire_core.models.upgrades import NodeUpgradeCollectiveCleanup

    scope = cleanup_scope()
    body = NodeUpgradeCollectiveCleanup(scope=scope).model_dump(mode="json")
    route = f"/node/upgrades/{scope.plan.operation.job_id}/collective-cleanup"
    with client(tmp_path) as node:
        assert node.post(route, json=body).status_code == 401
        assert node.post(route, json=body | {"command": "launch"}, headers=AUTH).status_code == 422
        assert (
            node.post(
                "/node/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/collective-cleanup",
                json=body,
                headers=AUTH,
            ).status_code
            == 409
        )
        assert not (tmp_path / "state/upgrades/collectives").exists()


def test_cleanup_route_durably_closes_and_replays_without_worker_result(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from coire_core.models.upgrades import (
        NODE_UPGRADE_OWNERS,
        NodeMaintenanceCommand,
        NodeUpgradeCollectiveCleanup,
        NodeUpgradeInventory,
        UpgradeInventoryEntry,
        UpgradeOwnerInventory,
    )
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node.upgrade_guard import NativeGuardTransport
    from coire_node.upgrade_inventory import collective_entries

    scope = cleanup_scope()

    def snapshot(node, runtime, local):
        return NodeUpgradeInventory(
            job_id=scope.plan.operation.job_id,
            generation=scope.request.generation,
            node=node,
            runtime_sha256=runtime,
            observed_at=datetime.now(UTC),
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=name,
                    status="complete",
                    entries=(
                        UpgradeInventoryEntry(
                            owner="probes",
                            kind="upgrade-collective",
                            work_id=str(scope.command_id),
                            state="cleaning",
                            holds=("unresolved-collective",),
                        ),
                    )
                    if local and name == "probes"
                    else (),
                )
                for name in sorted(NODE_UPGRADE_OWNERS)
            ),
        )

    fetch = Mock(
        return_value=(
            snapshot("coire-edge-a", scope.runtime_pair()[0], True),
            snapshot("coire-edge-b", scope.runtime_pair()[1], False),
        )
    )
    monkeypatch.setattr(NativeGuardTransport, "cleanup_inventories", fetch)
    with client(tmp_path) as node:
        gate = node.app.state.maintenance
        gate.apply(NodeMaintenanceCommand(maintenance=scope.plan.maintenance))
        root = gate.store
        for part in (
            "collectives",
            scope.plan.operation.job_id,
            scope.request.stage,
            str(scope.index),
        ):
            root = PrivateUpgradeStore(root.path(part))
        root.write("scope.json", scope, replace=False)
        body = NodeUpgradeCollectiveCleanup(scope=scope).model_dump(mode="json")
        route = f"/node/upgrades/{scope.plan.operation.job_id}/collective-cleanup"
        first = node.post(route, json=body, headers=AUTH)
        assert first.status_code == 200, first.text
        assert node.post(route, json=body, headers=AUTH).json() == first.json()
        fetch.assert_called_once()
        assert gate.blocked()
        assert not root.path("result.json").exists()
        assert collective_entries(tmp_path / "state") == ()


def test_status_is_authenticated_and_truthfully_not_ready(tmp_path: Path) -> None:
    with client(tmp_path) as node:
        assert node.get("/node/upgrades/status").status_code == 401
        response = node.get("/node/upgrades/status", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["bootstrap_ready"] is False
        assert response.json()["runtime_sha256"] is None
        assert not (tmp_path / "bootstrap").exists()


def test_unready_execute_and_unknown_command_fields_have_no_effects(tmp_path: Path) -> None:
    value = plan()
    with client(tmp_path) as node:
        route = "/node/upgrades/" + value.operation.job_id
        assert node.put(route, json=value.model_dump(mode="json")).status_code == 401
        response = node.put(route, json=value.model_dump(mode="json"), headers=AUTH)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
        assert (
            node.put(
                route,
                json=value.model_dump(mode="json") | {"path": "/foreign", "command": "launch"},
                headers=AUTH,
            ).status_code
            == 422
        )
        assert not (tmp_path / "bootstrap").exists()


def test_maintenance_rejects_different_route_owner_before_persistence(tmp_path: Path) -> None:
    value = plan()
    with client(tmp_path) as node:
        response = node.put(
            "/node/maintenance/01ARZ3NDEKTSV4RRFFQ69G5FAW",
            json={"maintenance": value.maintenance.model_dump(mode="json")},
            headers=AUTH,
        )
        assert response.status_code == 409
        assert not (tmp_path / "state/upgrades/maintenance.json").exists()


@pytest.mark.parametrize("fault", ["source_digest", "source_configuration", "route_owner"])
def test_malformed_source_and_route_authority_are_rejected_before_bootstrap(
    tmp_path: Path, fault: str
) -> None:
    value = plan()
    payload = value.model_dump(mode="json")
    route_job = value.operation.job_id
    if fault == "source_digest":
        payload["source"]["manifest_sha256"] = "f" * 64
    elif fault == "source_configuration":
        payload["source"]["manifest"]["environment_sha256"] = "f" * 64
    else:
        route_job = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    with client(tmp_path) as node:
        response = node.put("/node/upgrades/" + route_job, json=payload, headers=AUTH)
        assert response.status_code == (409 if fault == "route_owner" else 422)
        assert not (tmp_path / "bootstrap").exists()
        assert not (tmp_path / "state/upgrades/claim.json").exists()


@pytest.mark.parametrize("method, suffix", [("GET", ""), ("PUT", ""), ("POST", "/cancel")])
def test_every_operation_delivery_requires_the_node_credential(
    tmp_path: Path, method: str, suffix: str
) -> None:
    value = plan()
    payload = (
        {"generation": value.operation.generation} if suffix else value.model_dump(mode="json")
    )
    with client(tmp_path) as node:
        response = node.request(
            method,
            "/node/upgrades/" + value.operation.job_id + suffix,
            json=payload,
            headers={"Authorization": "Bearer private-invalid-token"},
        )
        assert response.status_code == 401
        assert "private-invalid-token" not in response.text
        assert not (tmp_path / "bootstrap").exists()


@pytest.mark.parametrize("fault", [None, "unknown", "changed_source", "missing_source_proof"])
def test_unchanged_abort_preserves_existing_requests_only_after_source_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import AsyncMock, Mock

    from fastapi import FastAPI

    from coire_core.errors import UpgradeUnavailable
    from coire_core.models.upgrades import (
        NodeUpgradeInventory,
        UpgradeInventoryEntry,
        UpgradeOwnerInventory,
    )
    from coire_node.maintenance import REQUIRED_OWNERS

    value = plan()
    runtime = value.source.manifest.environment_sha256
    assert runtime is not None
    with client(tmp_path) as node:
        app = cast(FastAPI, node.app)
        gate = app.state.maintenance
        control = app.state.upgrades
        observed = SimpleNamespace(runtime=SimpleNamespace(environment_sha256=runtime))
        monkeypatch.setattr(control, "observed", Mock(return_value=observed))
        verified = Mock(
            side_effect=UpgradeUnavailable("source changed") if fault == "changed_source" else None
        )
        monkeypatch.setattr(control, "verify_unchanged_source", verified)
        monkeypatch.setattr(control, "verify_terminal", Mock())
        monkeypatch.setattr(control, "release_claim", Mock())
        snapshot = NodeUpgradeInventory(
            job_id=value.operation.job_id,
            generation=1,
            node="coire-edge-a",
            runtime_sha256=runtime,
            observed_at=datetime.now(UTC),
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=name,
                    status="unknown" if name == "training" and fault == "unknown" else "complete",
                    entries=(
                        UpgradeInventoryEntry(
                            owner=name, kind="request", work_id="existing", state="active"
                        ),
                    )
                    if name == "requests"
                    else (),
                )
                for name in REQUIRED_OWNERS
            ),
        )
        monkeypatch.setattr(gate, "inventory_async", AsyncMock(return_value=snapshot))
        route = "/node/maintenance/" + value.operation.job_id
        with gate.admission():
            assert (
                node.put(
                    route,
                    json={"maintenance": value.maintenance.model_dump(mode="json")},
                    headers=AUTH,
                ).status_code
                == 200
            )
            body = {
                "maintenance": value.maintenance.model_dump(mode="json"),
                "action": "release",
                "terminal_verified": True,
            }
            if fault != "missing_source_proof":
                body.update(source_unchanged=True, source_runtime_sha256=runtime)
            response = node.put(route, json=body, headers=AUTH)
            assert response.status_code == (
                200 if fault is None else (503 if fault == "changed_source" else 409)
            )
            assert (gate.current().state == "released") == (fault is None)
            if fault is None:
                verified.assert_called_once_with(value.operation.job_id, 1, runtime)
                with gate.admission():
                    pass
            else:
                control.release_claim.assert_not_called()


def test_core_smoke_scope_uses_authenticated_normal_engine_proxy_without_forwarding_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import Mock
    from uuid import uuid4

    import httpx
    from upgrade_fixtures import core_plan

    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.engine import EngineState, EngineStatus
    from coire_core.models.upgrades import CoreNodeSmokeLifecycle, CoreNodeSmokeScope
    from coire_node import core_smoke
    from coire_node.engines import EngineManager
    from coire_node.routes import engines as routes

    value = core_plan()
    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        node_token=SecretStr(TOKEN),
        legacy_network_mode=False,
        node_state_dir=str(tmp_path / "state"),
    )
    gate = NodeMaintenance(tmp_path / "fence", "coire-edge-a")
    gate.store.write("maintenance.json", value.maintenance)
    scope = CoreNodeSmokeScope(
        plan=value,
        node="coire-edge-a",
        engine_id=uuid4(),
        runtime_sha256="a" * 64,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    core_smoke.core_smoke_store(settings, scope).write(
        "lifecycle.json", CoreNodeSmokeLifecycle(scope=scope, updated_at=datetime.now(UTC))
    )
    status = EngineStatus(
        engine_id=scope.engine_id,
        slug=value.model_slug,
        port=39210,
        state=EngineState.READY,
        started_at=datetime.now(UTC),
        target=InferenceTarget(
            model_id=value.model_id,
            variant_id=value.model_variant_id,
            base_manifest_sha256=value.model_manifest_sha256,
        ),
    )
    manager = cast(EngineManager, SimpleNamespace(get=Mock(return_value=status)))
    store = Store(tmp_path / "models")
    monkeypatch.setattr(
        store,
        "read_manifest",
        lambda _slug: SimpleNamespace(
            total_bytes=1024,
            sha256=lambda: value.model_manifest_sha256,
        ),
    )
    monkeypatch.setattr(core_smoke, "running_runtime_sha256", lambda _settings: "a" * 64)
    forwarded = []

    def upstream(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        assert "coire_upgrade_scope" not in payload
        assert payload["model"] == str(store.path_for(value.model_slug))
        assert gate._admitting == 1
        forwarded.append(payload)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(routes, "_engine_client", lambda: transport)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=store,
        engines=manager,
        maintenance=gate,
    )
    request = core_smoke.fixed_smoke_request(scope).model_dump(mode="json", exclude_none=True)
    path = f"/node/engines/{scope.engine_id}/proxy/v1/chat/completions"
    with TestClient(app) as api:
        assert api.post(path, json=request).status_code == 401
        normal = dict(request)
        normal.pop("coire_upgrade_scope")
        assert api.post(path, json=normal, headers=AUTH).status_code == 503
        assert api.post(path, json=request, headers=AUTH).status_code == 200
        assert len(forwarded) == 1
        assert gate._admitting == 0
        assert gate.blocked()
        request["max_tokens"] = 65
        assert api.post(path, json=request, headers=AUTH).status_code == 409
        assert len(forwarded) == 1


def test_core_smoke_engine_start_keeps_normal_lifecycle_admission_in_its_exact_thread_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import Mock
    from uuid import uuid4

    from upgrade_fixtures import core_plan

    from coire_core.errors import UpgradeUnavailable
    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.engine import EngineStartRequest, EngineState, EngineStatus
    from coire_core.models.upgrades import CoreNodeSmokeScope
    from coire_node import core_smoke
    from coire_node.engines import EngineManager

    value = core_plan()
    settings = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        node_token=SecretStr(TOKEN),
        legacy_network_mode=False,
        node_state_dir=str(tmp_path / "state"),
    )
    gate = NodeMaintenance(tmp_path / "fence", "coire-edge-a")
    gate.store.write("maintenance.json", value.maintenance)
    scope = CoreNodeSmokeScope(
        plan=value,
        node="coire-edge-a",
        engine_id=uuid4(),
        runtime_sha256="a" * 64,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    target = InferenceTarget(
        model_id=value.model_id,
        variant_id=value.model_variant_id,
        base_manifest_sha256=value.model_manifest_sha256,
    )
    status = EngineStatus(
        engine_id=scope.engine_id,
        slug=value.model_slug,
        port=39210,
        state=EngineState.STARTING,
        started_at=datetime.now(UTC),
        target=target,
    )
    invoked = []

    def start(**_kwargs: object) -> tuple[bool, EngineStatus]:
        # asyncio.to_thread must carry only this checked request's private scope.
        gate.check()
        assert gate._admitting == 1
        invoked.append(True)
        return False, status

    manager = cast(EngineManager, SimpleNamespace(get=Mock(return_value=None), start=start))
    store = Store(tmp_path / "models")
    monkeypatch.setattr(store, "verify_against", lambda _slug, _manifest: [])
    monkeypatch.setattr(
        store,
        "read_manifest",
        lambda _slug: SimpleNamespace(
            total_bytes=1024,
            sha256=lambda: value.model_manifest_sha256,
        ),
    )
    monkeypatch.setattr(core_smoke, "running_runtime_sha256", lambda _settings: "a" * 64)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=store,
        engines=manager,
        maintenance=gate,
    )
    body = EngineStartRequest(
        engine_id=scope.engine_id,
        slug=value.model_slug,
        target=target,
        estimate_bytes=1024**3,
        coire_upgrade_scope=scope,
    ).model_dump(mode="json", exclude_none=True)
    with TestClient(app) as api:
        assert api.post("/node/engines", json=body).status_code == 401
        assert api.post("/node/engines", json=body, headers=AUTH).status_code == 202
        assert invoked == [True]
        assert gate._admitting == 0
        with pytest.raises(UpgradeUnavailable):
            gate.check()
        body["estimate_bytes"] = 3 * 1024**3
        assert api.post("/node/engines", json=body, headers=AUTH).status_code == 409
        assert invoked == [True]


def test_lost_accept_response_observes_the_original_native_publication_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual authenticated route/journal; isolated guard/catalog attestation boundaries."""
    import os
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx
    import psutil
    from upgrade_native_fixtures import native_plan

    from coire_core.models.upgrades import (
        NativeGuardStatus,
        NativeNodeProcess,
        NativeUpgradeClaim,
        NodeAgentProcessIdentity,
        NodeUpgradeCommand,
    )
    from coire_core.upgrade_journal import PrivateUpgradeStore
    from coire_node import upgrade_control

    value, selected, _ = native_plan(tmp_path)
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    bootstrap.write("runtime.json", selected.runtime)
    bootstrap.write("selection.json", selected)
    identity = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process().create_time(),
        environment_sha256=selected.runtime.environment_sha256,
    )
    bootstrap.write(
        "status.json",
        NativeGuardStatus(
            node="coire-edge-a",
            bootstrap_runtime=selected.runtime,
            process=identity,
            selected=selected,
            ready=True,
            observed_at=datetime.now(UTC),
        ),
    )
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    state.write(
        "node-process.json",
        NativeNodeProcess(selected=selected, process=identity, recorded_at=datetime.now(UTC)),
    )
    monkeypatch.setattr(
        upgrade_control,
        "sys",
        SimpleNamespace(
            prefix=str(tmp_path / "envs/source"),
            executable=str(tmp_path / "envs/source/bin/python3"),
        ),
    )
    monkeypatch.setattr(upgrade_control, "_owned_native_process", lambda *_: Mock())
    monkeypatch.setattr(
        upgrade_control,
        "verify_prepared_release",
        lambda _root, request: (
            value.source
            if request.expected_sha256 == value.source.manifest_sha256
            else value.target
        ),
    )
    configured = Settings(
        node_name="coire-edge-a",
        upgrades_enabled=True,
        node_token=SecretStr(TOKEN),
        legacy_network_mode=False,
        node_state_dir=str(tmp_path / "state"),
        upgrade_bootstrap_root=str(bootstrap.root),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    app = create_app(
        configured,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(tmp_path / "models"),
        maintenance=NodeMaintenance(tmp_path / "state/upgrades", "coire-edge-a"),
    )
    command = NodeUpgradeCommand.model_validate(value.model_dump(mode="json"))
    route = "/node/upgrades/" + value.operation.job_id
    try:
        with TestClient(app) as node:

            def lost_response(request: httpx.Request) -> httpx.Response:
                response = node.put(
                    route,
                    content=request.content,
                    headers=AUTH | {"Content-Type": "application/json"},
                )
                assert response.status_code == 202, response.text[:1000]
                assert bootstrap.read("claim.json", NativeUpgradeClaim) is not None
                raise httpx.ReadTimeout("fixture discards response after durable publication")

            with (
                httpx.Client(transport=httpx.MockTransport(lost_response)) as transport,
                pytest.raises(httpx.ReadTimeout),
            ):
                transport.put("http://node" + route, json=command.model_dump(mode="json"))
            claim_bytes = bootstrap.path("claim.json").read_bytes()
            resumed = node.get(route, headers=AUTH)
            assert resumed.status_code == 200
            assert resumed.json()["job_id"] == command.operation.job_id
            assert resumed.json()["receipt_sequence"] == 0
            replay = node.put(route, json=command.model_dump(mode="json"), headers=AUTH)
            assert replay.status_code == 202
            assert replay.json()["generation"] == command.operation.generation
            assert bootstrap.path("claim.json").read_bytes() == claim_bytes
            assert app.state.upgrades.journal(value.operation.job_id).current().phase == "prepared"
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
