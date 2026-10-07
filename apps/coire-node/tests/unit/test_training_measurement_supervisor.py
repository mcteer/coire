"""Probe intent and full slot accounting survive uncertain native spawn."""

import asyncio
import contextlib
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from training_measurement_fixtures import experiment

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    TrainingLeaseRenewal,
    TrainingStartRequest,
    TrainingStopRequest,
)
from coire_node.training.journal import TrainingJournal
from coire_node.training.measurement import MeasurementSupervisor
from coire_node.training.supervisor import TrainingSupervisor


@pytest.mark.parametrize("owner", [MeasurementSupervisor, TrainingSupervisor])
async def test_watchdog_journal_wait_keeps_request_loop_responsive(
    owner: type[TrainingSupervisor],
) -> None:
    """An occupied journal must not prevent health or lease requests from running."""
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BusyJournal:
        def records(self) -> list[dict[str, object]]:
            entered.set()
            release.wait(timeout=1)
            finished.set()
            return []

    supervisor = object.__new__(owner)
    supervisor.journal = BusyJournal()  # type: ignore[assignment]
    task = asyncio.create_task(supervisor.watchdog())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
        await asyncio.sleep(0)
        assert not finished.is_set(), "Journal access blocked the request event loop"
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_lease_write_wait_keeps_loop_responsive_and_serializes_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def occupied_write(command: TrainingLeaseRenewal) -> None:
        entered.set()
        release.wait(timeout=1)
        finished.set()

    _, dispatch = experiment()
    prepare = dispatch.commands[0].prepare
    command = TrainingLeaseRenewal(
        **{name: getattr(prepare, name) for name in TrainingLeaseRenewal.model_fields}
    )
    supervisor = object.__new__(MeasurementSupervisor)
    supervisor.lock = asyncio.Lock()
    monkeypatch.setattr(supervisor, "_renew", occupied_write)
    task = asyncio.create_task(supervisor.renew(command))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
        assert not finished.is_set(), "Lease write blocked the request event loop"
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done() and supervisor.lock.locked()
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert finished.is_set() and not supervisor.lock.locked()


async def test_guard_refuses_before_any_hold_and_probe_uses_fixed_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CPU journal test only: no inputs, tokenizer, process spawn or Metal execution.
    monkeypatch.setattr("coire_node.training.measurement.platform.node", lambda: "coire-edge-a")
    _, dispatch = experiment()
    probe = dispatch.commands[0]
    journal = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )

    def refuse(command: object) -> None:
        raise TrainingConflict("active accelerator")

    supervisor = MeasurementSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/pinned/bin/python"),
        accelerator_guard=refuse,
        store_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        hardware_sha256=lambda: probe.hardware_sha256,
        memory_available=lambda: 16 * 1024**3,
        disk_available=lambda: 100 * 1024**3,
    )
    with pytest.raises(TrainingConflict):
        await supervisor.prepare_measurement(probe)
    assert journal.records() == []
    argv = supervisor.argv(
        {"attempt_id": probe.prepare.attempt_id, "spawn_nonce": dispatch.spawn_nonce}
    )
    assert argv[1:3] == ["-m", "coire_node.training.measurement"]
    assert "--model" not in argv
    assert argv[-1] == str(tmp_path / "artifacts" / probe.prepare.attempt_id)
    journal.close()


async def test_probe_ceiling_and_immutable_config_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coire_node.training.measurement.platform.node", lambda: "coire-edge-a")
    _, dispatch = experiment()
    probe = dispatch.commands[0]
    journal = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )

    def supervisor(j: TrainingJournal) -> MeasurementSupervisor:
        return MeasurementSupervisor(
            j,
            interpreter=Path("/opt/coire/envs/pinned/bin/python"),
            accelerator_guard=lambda _: None,
            hardware_sha256=lambda: probe.hardware_sha256,
            store_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            memory_available=lambda: 16 * 1024**3,
            disk_available=lambda: 100 * 1024**3,
        )

    runtime = supervisor(journal)
    receipt = await runtime.prepare_measurement(probe)
    assert not receipt.ready
    assert journal.held_bytes()[0] == probe.prepare.resolved.resource_envelope.memory_bytes
    journal.close()
    restarted = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )
    assert restarted.held_bytes()[0] == 8 * 1024**3
    runtime = supervisor(restarted)
    assert runtime.probe(probe.prepare.attempt_id) == probe
    altered = probe.model_copy(deep=True)
    altered.hardware_sha256 = "b" * 64
    with pytest.raises(TrainingConflict):
        await runtime.prepare_measurement(altered)
    assert len(restarted.records()) == 1
    restarted.close()


async def test_core_refuses_before_hold_or_tokenizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coire_node.training.measurement.platform.node", lambda: "coire-core")
    _, dispatch = experiment()
    probe = dispatch.commands[0]
    journal = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )
    runtime = MeasurementSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/pinned/bin/python"),
        accelerator_guard=lambda _: None,
        hardware_sha256=lambda: probe.hardware_sha256,
        store_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        memory_available=lambda: 16 * 1024**3,
        disk_available=lambda: 100 * 1024**3,
    )
    with pytest.raises(TrainingValidationError, match="forbidden on core"):
        await runtime.prepare_measurement(probe)
    assert journal.records() == []
    assert runtime.capabilities().world_sizes == []
    journal.close()


@pytest.mark.parametrize("expired", [False, True])
async def test_rejected_prepare_survives_restart_and_proves_fenced_no_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    monkeypatch.setattr("coire_node.training.measurement.platform.node", lambda: "coire-edge-a")
    monkeypatch.setattr("coire_node.training.measurement.attempt_process_absent", lambda _: True)
    _, dispatch = experiment()
    probe = dispatch.commands[0]

    if expired:
        probe.prepare.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    def refused(command: object) -> None:
        assert not expired  # Expired preparation cannot reach accelerator inspection.
        raise TrainingConflict("unresolved accelerator inventory")

    def supervisor(journal: TrainingJournal) -> MeasurementSupervisor:
        return MeasurementSupervisor(
            journal,
            interpreter=Path("/opt/coire/envs/pinned/bin/python"),
            accelerator_guard=refused,
            store_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            hardware_sha256=lambda: probe.hardware_sha256,
            memory_available=lambda: 16 * 1024**3,
            disk_available=lambda: 100 * 1024**3,
        )

    journal = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )
    monkeypatch.setattr("coire_node.training.measurement.attempt_process_absent", lambda _: False)
    with pytest.raises(TrainingConflict):
        await supervisor(journal).prepare_measurement(probe)
    assert not (journal.root / f"rejected-measurement-{probe.prepare.attempt_id}.json").exists()
    monkeypatch.setattr("coire_node.training.measurement.attempt_process_absent", lambda _: True)
    with pytest.raises(TrainingConflict):
        await supervisor(journal).prepare_measurement(probe)
    assert journal.records() == [] and journal.held_bytes() == (0, 0)
    journal.close()
    journal = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )
    runtime = supervisor(journal)
    fields = {
        "command_id": uuid.uuid4(),
        "job_id": probe.prepare.job_id,
        "attempt_id": probe.prepare.attempt_id,
        "fence": probe.prepare.fence,
        "request_sha256": probe.prepare.request_sha256,
        "rank": probe.prepare.rank,
        "world_size": probe.prepare.world_size,
        "node": probe.prepare.node,
        "lease_expires_at": datetime.now(UTC) + timedelta(seconds=30),
    }
    stop = TrainingStopRequest.model_validate({**fields, "reason": "cancelled"})
    with pytest.raises(TrainingConflict):
        await runtime.stop(stop.model_copy(update={"fence": stop.fence + 1}))
    receipt = await runtime.stop(stop)
    assert receipt.stopped and receipt.pid is None
    assert await runtime.stop(stop) == receipt
    with pytest.raises(TrainingConflict):
        await runtime.stop(stop.model_copy(update={"request_sha256": "f" * 64}))
    assert runtime.measurement_status(stop.attempt_id).stopped
    with pytest.raises(TrainingConflict):
        await runtime.prepare_measurement(probe)
    start = TrainingStartRequest.model_validate(
        {
            **fields,
            "prepared_command_id": probe.prepare.command_id,
            "spawn_nonce": dispatch.spawn_nonce,
        }
    )
    with pytest.raises(TrainingConflict):
        await runtime.start(start)
    assert journal.held_bytes() == (0, 0)
    monkeypatch.setattr("coire_node.training.measurement.attempt_process_absent", lambda _: False)
    with pytest.raises(TrainingConflict, match="ownership"):
        await runtime.stop(stop)
    monkeypatch.setattr("coire_node.training.measurement.attempt_process_absent", lambda _: True)
    # Unexpected local ownership invalidates the no-start proof, even after a
    # previously successful stop. A missing/corrupt namespace is never guessed away.
    (journal.root / stop.attempt_id).mkdir()
    with pytest.raises(TrainingConflict, match="ownership"):
        await runtime.stop(stop)
    journal.close()
