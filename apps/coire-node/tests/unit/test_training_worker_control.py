"""Model-free checks for training deadlines, sampler partition and finite output."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training import LearningRateSchedule, TrainingOptimizer
from coire_node.training.guard import ExecutionGuard
from coire_node.training.worker import finite_loss, learning_rate, partition_batch


@pytest.mark.parametrize("mixed", [False, True])
def test_measurement_cannot_resume_or_mix_durable_commit_before_model_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mixed: bool
) -> None:
    from training_measurement_fixtures import experiment

    from coire_node.training.worker import run_sft

    monkeypatch.setattr("coire_node.training.worker.platform.node", lambda: "coire-edge-a")
    prepared = experiment()[1].commands[0].prepare
    unavailable: Any = None

    def forbidden(*args: Any) -> Any:
        raise AssertionError("invalid measurement mode reached execution")

    with pytest.raises(TrainingValidationError, match="Measurement cannot mix"):
        run_sft(
            prepared,
            unavailable,
            unavailable,
            unavailable,
            unavailable,
            store=unavailable,
            scratch=tmp_path,
            emit=forbidden,
            commit=forbidden,
            control=lambda: "continue",
            footprint=lambda: 1,
            completed_update=0 if mixed else 1,
            checkpoint=forbidden if mixed else None,
            measurement_checkpoint=forbidden,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1"])
def test_nonfinite_or_untyped_loss_refused(value: object) -> None:
    with pytest.raises(TrainingValidationError):
        finite_loss(value)


def test_monotonic_lease_pause_cancel_deadlines() -> None:
    now = [100.0]
    guard = ExecutionGuard(datetime.now(UTC) + timedelta(seconds=29), clock=lambda: now[0])
    guard.pause()
    assert guard.action() == "pause"
    guard.cancel()
    assert guard.action() == "cancel"
    now[0] += 5
    assert guard.action() == "kill"
    now[0] += 30
    with pytest.raises(TrainingConflict, match="resurrected"):
        guard.renew(datetime.now(UTC) + timedelta(seconds=29))


def test_repeated_pause_cannot_extend_deadline() -> None:
    now = [0.0]
    guard = ExecutionGuard(datetime.now(UTC) + timedelta(seconds=29), clock=lambda: now[0])
    guard.pause()
    deadline = guard.pause_deadline
    now[0] = 10
    guard.pause()
    assert guard.pause_deadline == deadline


def test_pause_kill_deadline_survives_healthy_lease_renewals() -> None:
    now = [0.0]
    guard = ExecutionGuard(datetime.now(UTC) + timedelta(seconds=29), clock=lambda: now[0])
    guard.pause()
    for tick in [20.0, 40.0, 59.0]:
        now[0] = tick
        guard.renew(datetime.now(UTC) + timedelta(seconds=29))
        assert guard.action() == "pause"
    now[0] = 60.0
    assert guard.action() == "kill"


def test_deterministic_rank_slices_have_no_overlap_or_dropped_rows() -> None:
    batch = SimpleNamespace(tokens=[[i, 9] for i in range(4)], target_masks=[[False, True]] * 4)
    first = partition_batch(batch, rank=0, world_size=2)
    second = partition_batch(batch, rank=1, world_size=2)
    assert first[0] + second[0] == batch.tokens
    with pytest.raises(TrainingValidationError):
        partition_batch(SimpleNamespace(tokens=[[1]], target_masks=[[True]]), rank=0, world_size=2)


def test_schedule_reconstruction_uses_completed_optimizer_steps() -> None:
    settings = TrainingOptimizer(
        updates=6,
        learning_rate=0.6,
        schedule=LearningRateSchedule(kind="warmup_linear", warmup_updates=2),
    )
    mx = SimpleNamespace(maximum=max, where=lambda condition, yes, no: yes if condition else no)
    assert [learning_rate(settings, step, mx) for step in range(7)] == pytest.approx(
        [0.3, 0.6, 0.6, 0.45, 0.3, 0.15, 0.0]
    )
