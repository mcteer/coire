"""Refused ordinary preparations have restartable fenced no-start proof."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from training_measurement_fixtures import experiment

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import TrainingStartRequest, TrainingStopRequest
from coire_node.training.journal import TrainingJournal
from coire_node.training.supervisor import TrainingSupervisor


@pytest.mark.parametrize("expired", [False, True])
async def test_refused_prepare_fences_no_start_and_replays_stop_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    monkeypatch.setattr("coire_node.training.rejections.attempt_process_absent", lambda _: True)
    prepared = experiment()[1].commands[0].prepare
    if expired:
        prepared.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    def supervisor(journal: TrainingJournal) -> TrainingSupervisor:
        return TrainingSupervisor(
            journal,
            interpreter=Path("/opt/coire/envs/test/bin/python3"),
            memory_available=lambda: 0,
            disk_available=lambda: 100 * 1024**3,
        )

    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    with pytest.raises((TrainingConflict, TrainingValidationError)):
        await supervisor(journal).prepare(prepared)
    assert journal.records() == [] and journal.held_bytes() == (0, 0)
    journal.close()
    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    runtime = supervisor(journal)
    fields = {
        k: getattr(prepared, k)
        for k in ("job_id", "attempt_id", "fence", "node", "rank", "world_size", "request_sha256")
    }
    fields.update(
        command_id=uuid.uuid4(), lease_expires_at=datetime.now(UTC) + timedelta(seconds=29)
    )
    stop = TrainingStopRequest.model_validate({**fields, "reason": "cancelled"})
    with pytest.raises(TrainingConflict, match="scope"):
        await runtime.stop(stop.model_copy(update={"fence": stop.fence + 1}))
    receipt = await runtime.stop(stop)
    assert receipt.stopped and receipt.pid is None
    assert await runtime.stop(stop) == receipt
    runtime.release_stopped_attempt(stop.attempt_id)
    assert runtime.observe(stop.attempt_id).liveness == "stopped"
    start = TrainingStartRequest.model_validate(
        {**fields, "prepared_command_id": prepared.command_id, "spawn_nonce": uuid.uuid4()}
    )
    with pytest.raises(TrainingConflict, match="cannot start"):
        await runtime.start(start)
    with pytest.raises(TrainingConflict, match="durably rejected"):
        await runtime.prepare(prepared)
    monkeypatch.setattr("coire_node.training.rejections.attempt_process_absent", lambda _: False)
    with pytest.raises(TrainingConflict, match="unresolved"):
        await runtime.stop(stop)
    journal.close()


async def test_stop_before_prepare_persists_no_start_proof_and_blocks_late_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coire_node.training.rejections.attempt_process_absent", lambda _: True)
    prepared = experiment()[1].commands[0].prepare
    fields = {
        k: getattr(prepared, k)
        for k in ("job_id", "attempt_id", "fence", "node", "rank", "world_size", "request_sha256")
    }
    stop = TrainingStopRequest(
        **fields,
        command_id=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=29),
        reason="cancelled",
    )
    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    runtime = TrainingSupervisor(journal, interpreter=Path("/opt/coire/envs/test/bin/python3"))
    receipt = await runtime.stop(stop)
    assert receipt.stopped and receipt.pid is None
    assert not journal.records() and journal.held_bytes() == (0, 0)
    journal.close()
    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    runtime = TrainingSupervisor(journal, interpreter=Path("/opt/coire/envs/test/bin/python3"))
    assert await runtime.stop(stop) == receipt
    assert runtime.observe(stop.attempt_id).liveness == "stopped"
    with pytest.raises(TrainingConflict):
        await runtime.prepare(prepared)
    with pytest.raises(TrainingConflict, match="scope"):
        await runtime.stop(stop.model_copy(update={"fence": stop.fence + 1}))
    monkeypatch.setattr("coire_node.training.rejections.attempt_process_absent", lambda _: False)
    with pytest.raises(TrainingConflict, match="unresolved"):
        await runtime.stop(stop)
    journal.close()
