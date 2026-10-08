"""Node-owned evaluation preparation cannot accept paths or change an attempt."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from coire_core.evaluation_suites import template
from coire_core.models.adapters import InferenceTarget
from coire_core.models.evaluation import (
    EvaluationGeneration,
    EvaluationRuntime,
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkload,
    EvaluationWorkspacePrepare,
    canonical_digest,
)
from coire_core.models.registry import CapabilityProfile
from coire_core.settings import Settings
from coire_node.evaluations import EvaluationWorkspaceManager
from coire_node.workspaces import WorkspaceError


def workload() -> EvaluationWorkload:
    identity = InferenceTarget(
        model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
    )
    target = EvaluationTarget(
        target=identity,
        public_selector=str(identity.model_id),
        capability_profile=CapabilityProfile(),
        context_window=16384,
        runtime=EvaluationRuntime(
            engine_version="0.31.3",
            harness_version="0.1.0",
            runtime_sha256="b" * 64,
            tokenizer_sha256="c" * 64,
            template_sha256="d" * 64,
            capability_sha256="e" * 64,
        ),
        display_name="Fixture",
    )
    suite = EvaluationSuite(
        suite_id="harness-standard",
        version=1,
        template=template("harness-capability"),
        generation=EvaluationGeneration(),
        timeout_seconds=900,
        content_sha256="f" * 64,
        registered_at=datetime.now(UTC),
    )
    return EvaluationWorkload(
        evaluation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        fence=1,
        phase="harness",
        suite=suite,
        target=target,
        subject_index=0,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )


async def test_prepare_is_idempotent_and_fence_conflict_preserves_request(tmp_path: Path) -> None:
    manager = EvaluationWorkspaceManager(
        Settings(run_workspace_root=str(tmp_path), node_name="coire-edge-a")
    )
    request = workload()
    command = EvaluationWorkspacePrepare(workload=request, request_sha256=canonical_digest(request))
    receipt = await manager.prepare(command)
    assert await manager.prepare(command) == receipt
    original = (tmp_path / receipt.workspace_ref / ".coire" / "request.json").read_bytes()
    changed = request.model_copy(update={"fence": 2})
    with pytest.raises(WorkspaceError, match="differs"):
        await manager.prepare(
            EvaluationWorkspacePrepare(workload=changed, request_sha256=canonical_digest(changed))
        )
    assert (tmp_path / receipt.workspace_ref / ".coire" / "request.json").read_bytes() == original


async def test_interrupted_prepare_retries_without_an_unowned_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coire_node.evaluations as module
    from coire_node.store import write_atomic

    manager = EvaluationWorkspaceManager(
        Settings(run_workspace_root=str(tmp_path), node_name="coire-edge-a")
    )
    request = workload()
    command = EvaluationWorkspacePrepare(workload=request, request_sha256=canonical_digest(request))
    original = write_atomic

    def interrupt(path: Path, data: bytes) -> None:
        if path.name == "receipt.json":
            raise OSError("simulated process interruption")
        original(path, data)

    monkeypatch.setattr(module, "write_atomic", interrupt)
    with pytest.raises(OSError):
        await manager.prepare(command)
    monkeypatch.setattr(module, "write_atomic", original)
    receipt = await manager.prepare(command)
    assert await manager.prepare(command) == receipt


async def test_pressure_stop_is_idempotent_and_bound_to_measurement(tmp_path: Path) -> None:
    from coire_core.models.evaluation import EvaluationPressureBinding, EvaluationPressureStop

    manager = EvaluationWorkspaceManager(
        Settings(run_workspace_root=str(tmp_path), node_name="coire-edge-a")
    )
    request = workload().model_copy(
        update={"pressure": EvaluationPressureBinding(measurement_id=uuid.uuid4())}
    )
    receipt = await manager.prepare(
        EvaluationWorkspacePrepare(workload=request, request_sha256=canonical_digest(request))
    )
    assert request.pressure is not None
    stop = EvaluationPressureStop(
        run_id=request.run_id,
        measurement_id=request.pressure.measurement_id,
        request_sha256=receipt.request_sha256,
    )
    await manager.stop_pressure(stop)
    await manager.stop_pressure(stop)
    marker = (tmp_path / receipt.workspace_ref / ".coire" / "measurement-stop.json").read_bytes()
    with pytest.raises(WorkspaceError, match="ownership"):
        await manager.stop_pressure(stop.model_copy(update={"measurement_id": uuid.uuid4()}))
    assert (
        tmp_path / receipt.workspace_ref / ".coire" / "measurement-stop.json"
    ).read_bytes() == marker


async def test_symlink_and_catalog_tampering_are_rejected(tmp_path: Path) -> None:
    manager = EvaluationWorkspaceManager(
        Settings(run_workspace_root=str(tmp_path), node_name="coire-edge-a")
    )
    request = workload()
    (tmp_path / f"eval-{request.run_id}").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        await manager.prepare(
            EvaluationWorkspacePrepare(workload=request, request_sha256=canonical_digest(request))
        )
    request = workload()
    request.suite.template.content_sha256 = "0" * 64
    with pytest.raises(WorkspaceError):
        await manager.prepare(
            EvaluationWorkspacePrepare(workload=request, request_sha256=canonical_digest(request))
        )


def test_transport_refuses_path_version_and_bad_digest() -> None:
    request = workload()
    with pytest.raises(ValidationError):
        EvaluationWorkspacePrepare(workload=request, request_sha256="0" * 64)
    with pytest.raises(ValidationError):
        EvaluationWorkload.model_validate({**request.model_dump(), "workload_version": 2})
    with pytest.raises(ValidationError):
        EvaluationWorkload.model_validate({**request.model_dump(), "workspace": "../../secret"})


def test_routes_require_authenticated_node_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from coire_node.testing.harness import TOKEN, Agent

    agent = Agent(tmp_path)
    app = agent.app()
    app.state.runs = SimpleNamespace(
        evaluations=EvaluationWorkspaceManager(
            Settings(run_workspace_root=str(tmp_path / "workspaces"), node_name="coire-edge-a")
        )
    )
    request = workload()
    request.target.variant_slug = "synthetic-fixture"

    async def stub_identity(body: object, request_arg: object) -> EvaluationRuntime:
        return request.target.runtime

    monkeypatch.setattr("coire_node.routes.evaluations.identity", stub_identity)
    body = EvaluationWorkspacePrepare(
        workload=request, request_sha256=canonical_digest(request)
    ).model_dump(mode="json")
    with TestClient(app) as client:
        assert client.post("/node/evaluations/workspaces", json=body).status_code == 401
        assert (
            client.post(
                "/node/evaluations/workspaces",
                json=body,
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).status_code
            == 201
        )


async def test_input_staging_is_declared_digest_bound_and_idempotent(tmp_path: Path) -> None:
    import hashlib

    from coire_core.models.evaluation import EvaluationInputFile

    manager = EvaluationWorkspaceManager(
        Settings(run_workspace_root=str(tmp_path), node_name="coire-edge-a")
    )
    request = workload()
    payload = b'{"fixture":"private"}'
    request.input_files = [
        EvaluationInputFile(
            name="source.jsonl",
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload),
            purpose="previous_outputs",
        )
    ]
    command = EvaluationWorkspacePrepare(workload=request, request_sha256=canonical_digest(request))
    await manager.prepare(command)
    with pytest.raises(WorkspaceError):
        manager.validate_inputs(request.run_id)
    for name, digest, data in (
        ("../secret", command.request_sha256, payload),
        ("source.jsonl", "0" * 64, payload),
        ("source.jsonl", command.request_sha256, b"wrong"),
        ("source.jsonl", command.request_sha256, payload + b"oversized"),
    ):
        with pytest.raises(WorkspaceError):
            await manager.stage_input(request.run_id, digest, name, data)
    first = await manager.stage_input(
        request.run_id, command.request_sha256, "source.jsonl", payload
    )
    assert (
        await manager.stage_input(request.run_id, command.request_sha256, "source.jsonl", payload)
        == first
    )
    manager.validate_inputs(request.run_id)
    await manager.cleanup(request.run_id, command.request_sha256)
    assert not (tmp_path / first.name).exists()
