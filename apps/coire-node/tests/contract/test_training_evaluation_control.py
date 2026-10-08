"""A trainer applies only the negotiated checkpoint decision; kill wins over pause."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgement,
    CheckpointCommitAcknowledgementV2,
    EvaluationCheckpointPause,
)


def decision() -> CheckpointCommitAcknowledgementV2:
    return CheckpointCommitAcknowledgementV2(
        command_id=uuid.uuid4(),
        job_id="01J00000000000000000000001",
        attempt_id="01J00000000000000000000002",
        fence=1,
        request_sha256="a" * 64,
        node="coire-edge-a",
        rank=0,
        world_size=1,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=20),
        checkpoint_id=uuid.uuid4(),
        manifest_sha256="b" * 64,
        update=8,
        committed_update=8,
        job_version=4,
        evaluation_pause=EvaluationCheckpointPause(
            trigger_id=uuid.uuid4(), pause_command_id=uuid.uuid4()
        ),
    )


@pytest.mark.parametrize(
    "local,expected",
    [("continue", "pause"), ("pause", "pause"), ("cancel", "cancel"), ("kill", "kill")],
)
def test_checkpoint_evaluation_decision_applied_before_next_optimizer_update(
    local: str, expected: str
) -> None:
    from coire_node.training.worker import checkpoint_decision_action

    assert checkpoint_decision_action(decision(), local, spec_version=2) == expected


def test_v1_ack_is_unchanged_and_v2_requires_negotiated_decision() -> None:
    from coire_node.training.worker import checkpoint_decision_action

    data = decision().model_dump(mode="json")
    for name in ("committed_update", "job_version", "evaluation_pause"):
        data.pop(name)
    data["schema_version"] = 1
    legacy = CheckpointCommitAcknowledgement.model_validate(data)
    assert checkpoint_decision_action(legacy, "continue", spec_version=1) == "continue"
    with pytest.raises(TrainingConflict):
        checkpoint_decision_action(legacy, "continue", spec_version=2)
    with pytest.raises(TrainingConflict):
        checkpoint_decision_action(decision(), "continue", spec_version=1)


@pytest.mark.parametrize("change", ["protocol", "fence", "lease", "payload"])
async def test_checkpoint_decision_refuses_wrong_negotiation_or_replayed_authority(
    tmp_path: Path, change: str
) -> None:
    from training_measurement_fixtures import experiment

    from coire_node.training.journal import TrainingJournal
    from coire_node.training.supervisor import TrainingSupervisor

    _, dispatch = experiment()
    prepared = dispatch.commands[0].prepare
    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    try:
        journal.prepare(prepared, memory_available=1024**4, disk_available=1024**4, disk_floor=0)
        value = decision().model_copy(
            update={
                field: getattr(prepared, field)
                for field in (
                    "job_id",
                    "attempt_id",
                    "fence",
                    "request_sha256",
                    "node",
                    "rank",
                    "world_size",
                    "lease_expires_at",
                )
            }
        )
        if change == "fence":
            value = value.model_copy(update={"fence": prepared.fence + 1})
        elif change == "lease":
            value = value.model_copy(
                update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        elif change == "payload":
            with journal.transaction():
                journal.accept(value)
                journal.receipt(value, "committed")
            value = value.model_copy(update={"job_version": value.job_version + 1})
        supervisor = TrainingSupervisor(journal, interpreter=Path("/opt/coire/envs/v1/bin/python"))
        with pytest.raises(TrainingConflict, match="negotiated" if change == "protocol" else None):
            await supervisor.acknowledge_checkpoint(value)
    finally:
        journal.close()
