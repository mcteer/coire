"""Production mounts, disabled recovery and disjoint quota/health projections on CPU."""

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psutil
import pytest

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.acquisition import ReservationRequest
from coire_node.agent import AccountedArtifactImporter, guard_measurement, hardware_sha256, serve
from coire_node.metrics import MetricsCollector, read_swap_used_bytes
from coire_node.reservations import ReservationLedger, ReservationRefused, TrainingDiskBudget
from coire_node.testing.harness import TOKEN, Agent


@pytest.mark.asyncio
@pytest.mark.parametrize("use_default_reader", [False, True])
async def test_production_managers_mount_authenticate_and_recover_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_default_reader: bool,
) -> None:
    agent = Agent(
        tmp_path / "node",
        node_name="coire-edge-a",
        legacy_network_mode=False,
        training_enabled=False,
    )
    monkeypatch.setattr("coire_node.agent.resolve_control_address", lambda _: "127.0.0.1")
    monkeypatch.setattr("coire_node.agent.resolve_data_address", lambda *args: "127.0.0.2")
    monkeypatch.setattr("coire_node.agent.resolve_mesh_address", lambda *args: None)
    monkeypatch.setattr("coire_node.agent.resolve_egress_address", lambda: None)
    monkeypatch.setattr("coire_node.register.read_gpu_cores", lambda: 80)
    # CPU contract fixture simulates a declared Studio; no input/model is loaded or started.
    monkeypatch.setattr("coire_node.training.measurement.platform.node", lambda: "coire-edge-a")
    monkeypatch.setattr("coire_node.metrics.read_gpu_percent", lambda: None)
    monkeypatch.setattr("coire_node.metrics.read_thermal_state", lambda: "unknown")
    monkeypatch.setattr("coire_node.agent.psutil.process_iter", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "coire_node.agent.shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3)
    )
    # Other TestClients have independent event loops; this production lifecycle owns its client.
    monkeypatch.setattr("coire_node.routes.engines._proxy_client", httpx.AsyncClient())
    collector = MetricsCollector(
        node_name="coire-edge-a",
        agent_version="test",
        interval_s=100,
        budget_cpu_pct=100,
        budget_rss_bytes=1024**3,
        disk_path=str(tmp_path),
    )
    mounted: list[str] = []
    shutdown: list[str] = []
    readers = []
    fetched: list[str] = []
    if use_default_reader:
        from coire_node.training.lease_snapshot import TrainingLeaseSnapshotReader

        def fetch(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert request.headers["X-Coire-Node"] == "coire-edge-a"
            fetched.append(request.url.path)
            sampled = datetime.now(UTC)
            return httpx.Response(
                200,
                json={
                    "node": "coire-edge-a",
                    "sampled_at": sampled.isoformat(),
                    "expires_at": (sampled + timedelta(seconds=5)).isoformat(),
                    "active_leases": {},
                },
            )

        def reader(settings, *, node):  # type: ignore[no-untyped-def]
            value = TrainingLeaseSnapshotReader(
                settings, node=node, transport=httpx.MockTransport(fetch)
            )
            readers.append(value)
            return value

        monkeypatch.setattr("coire_node.agent.TrainingLeaseSnapshotReader", reader)

    async def inspect(server) -> None:  # type: ignore[no-untyped-def]
        app = server.config.app
        mounted.append(app.title)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://node"
        ) as client:
            path = "/node/training/measurements/01ARZ3NDEKTSV4RRFFQ69G5FAV"
            extraction = "/node/training/adapters/extractions"
            if "control" in app.title:
                assert app.state.training_rank_importer.reservations is app.state.reservations
                assert app.state.training_components is app.state.training.components
                assert app.state.training.jaccl_hostfile == Path(
                    agent.settings.sharding_jaccl_hostfile
                )
                original_rank_close = app.state.training_rank_importer.aclose
                original_training_close = app.state.training.aclose
                original_measurement_close = app.state.training_measurements.aclose

                async def close_rank() -> None:
                    assert app.state.training.journal.db.execute("SELECT 1").fetchone() == (1,)
                    shutdown.append("rank")
                    await original_rank_close()

                async def close_training() -> None:
                    shutdown.append("training")
                    await original_training_close()

                async def close_measurement() -> None:
                    shutdown.append("measurement")
                    await original_measurement_close()

                monkeypatch.setattr(app.state.training_rank_importer, "aclose", close_rank)
                monkeypatch.setattr(app.state.training, "aclose", close_training)
                monkeypatch.setattr(app.state.training_measurements, "aclose", close_measurement)
                assert isinstance(app.state.training_artifact_importer, AccountedArtifactImporter)
                assert "training-measurement-watchdog" in {
                    task.get_name() for task in asyncio.all_tasks()
                }
                assert (
                    app.state.training_measurements.journal.lock is app.state.training.journal.lock
                )
                assert (await client.get(path)).status_code == 401
                assert (await client.post(path + "/start", json={})).status_code == 401
                assert (await client.get(extraction + "/" + str(uuid.uuid4()))).status_code == 401
                assert (await client.post(extraction, json={})).status_code == 401
                headers = {"Authorization": f"Bearer {TOKEN}"}
                component = f"/node/training/components/{uuid.uuid4()}/ranks/0"
                assert (await client.get(component)).status_code == 401
                assert (await client.get(component, headers=headers)).status_code == 404
                assert (
                    await client.post("/node/training/components/imports", json={})
                ).status_code == 401
                assert (
                    await client.post(path + "/start", headers=headers, json={})
                ).status_code == 503
                assert (await client.post(extraction, headers=headers, json={})).status_code == 503
                assert (
                    await client.get(extraction + "/" + str(uuid.uuid4()), headers=headers)
                ).status_code == 404
                assert (await client.get(path, headers=headers)).status_code == 409
                health = await client.get("/node/health", headers=headers)
                assert health.status_code == 200 and "swap_used_bytes" in health.json()
                from test_training_lifecycle import envelope, prepare_command

                from coire_core.models.training_node import (
                    TrainingMeasurementPrepare,
                    TrainingStopRequest,
                )

                prepare = prepare_command()
                ceiling = app.state.engines.budget_bytes()
                prepare.resolved.resource_envelope.weight_bytes += (
                    ceiling - prepare.resolved.resource_envelope.memory_bytes
                )
                body = TrainingMeasurementPrepare(
                    measurement_id=uuid.uuid4(),
                    prepare=prepare,
                    hardware_sha256=hardware_sha256(agent.settings, 80),
                    mode="memory",
                    deadline=datetime.now(UTC) + timedelta(minutes=10),
                )
                agent.settings.training_enabled = True
                response = await client.post(
                    path + "/prepare", headers=headers, json=body.model_dump(mode="json")
                )
                assert response.status_code == 200 and not response.json()["ready"]
                assert app.state.training_measurements.committed_bytes() == ceiling
                request = ReservationRequest(
                    idempotency_key=uuid.uuid4(),
                    workflow_id=uuid.uuid4(),
                    variant_id=uuid.uuid4(),
                    memory_bytes=1,
                    disk_bytes=1,
                )
                with pytest.raises(ReservationRefused):
                    app.state.reservations.hold(request)
                ordinary = prepare.model_copy(
                    update={"attempt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW", "command_id": uuid.uuid4()}
                )
                with pytest.raises(TrainingValidationError):
                    await app.state.training.prepare(ordinary)
                assert app.state.engines._additional_committed_bytes() == ceiling
                projected = collector.sample()
                assert len(projected.training) == 1 and projected.memory_committed_bytes == ceiling
                agent.settings.training_enabled = False
                stop = TrainingStopRequest(
                    **{**envelope(prepare), "command_id": uuid.uuid4(), "reason": "cancelled"}
                )
                stopped = await client.post(
                    path + "/stop", headers=headers, json=stop.model_dump(mode="json")
                )
                assert stopped.status_code == 200 and stopped.json()["stopped"]
                assert app.state.training_measurements.committed_bytes() == 0
            else:
                assert (
                    await client.get(f"/node/training/components/{uuid.uuid4()}/ranks/0")
                ).status_code == 404
                assert (
                    await client.get(f"/training-components/{uuid.uuid4()}/ranks/0/manifest")
                ).status_code == 404
                assert (await client.get(path)).status_code == 404
                assert (await client.post(extraction, json={})).status_code == 404
                assert (
                    await client.get("/training-artifacts/" + str(uuid.uuid4()) + "/manifest")
                ).status_code == 404

    monkeypatch.setattr("coire_node.agent.uvicorn.Server.serve", inspect)
    try:
        await serve(
            agent.settings,
            collector,
            measurement_active_leases=None if use_default_reader else lambda _: 0,
        )
        assert len(mounted) == 2
        assert not any(
            task.get_name() == "training-measurement-watchdog" for task in asyncio.all_tasks()
        )
        assert shutdown == ["rank", "measurement", "training"]
        if use_default_reader:
            assert fetched and set(fetched) == {
                "/api/v1/internal/training/nodes/coire-edge-a/leases"
            }
            assert readers[0].client.is_closed
            assert readers[0].task is not None and readers[0].task.done()
            with pytest.raises(TrainingConflict):
                readers[0](set())
    finally:
        agent.close()


def test_swap_is_actual_or_unknown_and_hardware_matches_canonical_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    monkeypatch.setattr(
        "coire_node.metrics.psutil.swap_memory", lambda: SimpleNamespace(used=123456)
    )
    assert read_swap_used_bytes() == 123456

    def unavailable():  # type: ignore[no-untyped-def]
        raise OSError("unavailable")

    monkeypatch.setattr("coire_node.metrics.psutil.swap_memory", unavailable)
    assert read_swap_used_bytes() is None
    memory = psutil.virtual_memory().total
    from coire_api.db import NodeRow
    from coire_node import __version__
    from coire_scheduler.training_guard import hardware_digest

    # Registration and health advertise the installed node version, not the
    # unrelated shared control-plane SERVICE_VERSION default/override.
    agent.settings.service_version = "unrelated-control-plane-version"
    expected = hardware_digest(
        NodeRow(
            name="coire-edge-a", memory_total_bytes=memory, gpu_cores=80, agent_version=__version__
        )
    )
    try:
        assert hardware_sha256(agent.settings, 80) == expected
        with pytest.raises(TrainingConflict):
            hardware_sha256(agent.settings, None)
    finally:
        agent.close()


def test_artifact_quota_counts_materialized_hold_once_and_rejects_competing_scope(
    tmp_path: Path,
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    root = Path(agent.settings.node_state_dir) / "training"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    staging = artifacts / ".import-test"
    staging.mkdir()
    (staging / "bytes").write_bytes(b"x" * 20)
    lock = threading.RLock()
    budget = TrainingDiskBudget(root, lock, 100)
    ledger = ReservationLedger(
        agent.settings,
        agent.store,
        lambda: 0,
        memory_lock=lock,
        disk_quota_bytes=100,
        disk_usage_bytes=budget.committed_bytes,
    )
    budget.ledger = ledger
    first = ReservationRequest(
        idempotency_key=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        memory_bytes=1,
        disk_bytes=60,
    )
    try:
        ledger.hold(first, disk_path=artifacts, materialized_paths=(staging,))
        assert budget.committed_bytes() == 60  # Not 60 + 20.
        second = ReservationRequest(
            idempotency_key=uuid.uuid4(),
            workflow_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
            memory_bytes=1,
            disk_bytes=41,
        )
        with pytest.raises(ReservationRefused):
            ledger.hold(second, disk_path=artifacts)
        assert ledger.release(first.idempotency_key)
        assert budget.committed_bytes() == 20
    finally:
        agent.close()


def test_accelerator_guard_refuses_unknown_process_and_unobservable_gateway_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.models.engine import EngineState

    engines: Any = SimpleNamespace(find_orphans=lambda: [], statuses=lambda: [])
    vacant: Any = SimpleNamespace(committed_bytes=lambda: 0)
    reservations: Any = SimpleNamespace(held_bytes=lambda: 0)
    probe: Any = SimpleNamespace(prepare=SimpleNamespace(attempt_id="attempt"), mode="memory")
    unknown = SimpleNamespace(pid=99, cmdline=lambda: ["python", "-m", "mlx_lm.lora"])
    monkeypatch.setattr("coire_node.agent.psutil.process_iter", lambda: [unknown])
    with pytest.raises(TrainingConflict, match="unowned accelerator"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None)
    from psutil import AccessDenied

    def protected_argv() -> list[str]:
        raise AccessDenied(99)

    protected = SimpleNamespace(
        pid=99,
        cmdline=protected_argv,
        exe=lambda: "/usr/libexec/system-daemon",
        create_time=lambda: 1.0,
    )
    monkeypatch.setattr("coire_node.agent.psutil.process_iter", lambda: [protected])
    monkeypatch.setattr("coire_node.training.process_inventory.platform_code_flags", lambda _: None)
    with pytest.raises(TrainingConflict, match="inventory is unresolved"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None, lambda _: 0)
    monkeypatch.setattr(
        "coire_node.training.process_inventory.platform_code_flags", lambda _: 0x04000001
    )
    monkeypatch.setattr("coire_node.training.process_inventory.psutil.Process", lambda _: protected)
    guard_measurement(probe, engines, vacant, vacant, reservations, None, lambda _: 0)
    protected.exe = lambda: "/usr/bin/python3"
    with pytest.raises(TrainingConflict, match="inventory is unresolved"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None, lambda _: 0)
    monkeypatch.setattr("coire_node.agent.psutil.process_iter", lambda: [])
    with pytest.raises(TrainingConflict, match="lease inventory is unavailable"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None)
    with pytest.raises(TrainingConflict, match="leases are active"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None, lambda _: 1)
    guard_measurement(probe, engines, vacant, vacant, reservations, None, lambda _: 0)
    engine = SimpleNamespace(engine_id=uuid.uuid4(), target=None, pid=10, state=EngineState.READY)
    engines.statuses = lambda: [engine]
    probe.mode = "coexistence"
    instance_id = uuid.uuid4()
    probe.resident_targets = [SimpleNamespace(instance_id=instance_id, target=None)]
    probe.resident_engine_ids = {instance_id: engine.engine_id}
    with pytest.raises(TrainingConflict, match="lease inventory is unavailable"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None)
    with pytest.raises(TrainingConflict, match="leases are active"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None, lambda _: 1)
    observed: list[set[uuid.UUID]] = []

    def current_empty(ids: set[uuid.UUID]) -> int:
        observed.append(ids)
        return 0

    guard_measurement(probe, engines, vacant, vacant, reservations, None, current_empty)
    assert observed == [{instance_id}]
    probe.resident_engine_ids = {instance_id: uuid.uuid4()}
    with pytest.raises(TrainingConflict, match="Resident engine inventory"):
        guard_measurement(probe, engines, vacant, vacant, reservations, None, current_empty)


def test_measurement_health_memory_is_disjoint_and_duplicate_ownership_fails() -> None:
    collector = MetricsCollector(
        node_name="coire-edge-a",
        agent_version="test",
        interval_s=100,
        budget_cpu_pct=100,
        budget_rss_bytes=1024**3,
    )
    engines = SimpleNamespace(committed_bytes=lambda: 10, budget_bytes=lambda: 1000)
    image = SimpleNamespace(committed_bytes=lambda: 20)
    ledger = SimpleNamespace(held_bytes=lambda: 30)
    training = SimpleNamespace(
        committed_bytes=lambda: 40,
        statuses=lambda: [SimpleNamespace(attempt_id="training", footprint_bytes=11)],
    )
    measurements = SimpleNamespace(
        committed_bytes=lambda: 50,
        statuses=lambda: [SimpleNamespace(attempt_id="measurement", footprint_bytes=22)],
    )
    collector.attach(
        store=object(),
        jobs=object(),
        engines=engines,
        image_workers=image,
        reservations=ledger,
        training=training,
        measurements=measurements,
    )
    assert collector._committed() == 150
    assert [s.footprint_bytes for s in collector._training_statuses()] == [11, 22]
    measurements.statuses = training.statuses
    with pytest.raises(RuntimeError, match="conflicting"):
        collector._training_statuses()
