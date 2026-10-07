"""Persisted oldest-age reduction, bounded SDK series and failed-poll behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge, InMemoryMetricReader

from coire_api.training.telemetry import GUARD_REASONS, TRAINING_STATES, TrainingBaselineMetrics
from coire_scheduler import training_metrics
from coire_scheduler.training_metrics import AttemptObservation, reduce_training_metrics

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def attempt(
    *,
    state: str = "running",
    state_since: datetime = NOW - timedelta(seconds=400),
    unknown: bool = False,
    lease_expires_at: datetime = NOW + timedelta(seconds=30),
    last_update_at: datetime | None = None,
    stop_requested_at: datetime | None = None,
    pause_reason: str | None = None,
    current: bool = True,
) -> AttemptObservation:
    return AttemptObservation(
        "private-job",
        state,
        state_since,
        NOW - timedelta(seconds=500),
        unknown,
        lease_expires_at,
        last_update_at,
        stop_requested_at,
        None,
        pause_reason,
        current,
    )


def test_oldest_job_and_current_attempt_training_progress_win() -> None:
    result = reduce_training_metrics(
        now=NOW,
        jobs={"running": 2},
        attempts=[attempt(), attempt(last_update_at=NOW - timedelta(seconds=1))],
        recoveries=[NOW - timedelta(seconds=90), NOW - timedelta(seconds=10)],
        pending_checkpoints=[NOW - timedelta(seconds=70), NOW],
    )
    assert result.progress_oldest == 400
    assert result.recovery_oldest == 90
    assert result.checkpoint_pending_oldest == 70
    assert result.jobs["running"] == 2
    assert result.jobs["queued"] == 0


def test_old_fenced_attempt_does_not_become_current_progress() -> None:
    result = reduce_training_metrics(
        now=NOW,
        jobs={"running": 1},
        recoveries=[],
        pending_checkpoints=[],
        attempts=[attempt(current=False, unknown=True), attempt(last_update_at=NOW)],
    )
    assert result.progress_oldest == 0
    assert result.recovery_oldest == 500


def test_guard_thresholds_stop_proof_and_unknown_ownership() -> None:
    result = reduce_training_metrics(
        now=NOW,
        jobs={},
        recoveries=[],
        pending_checkpoints=[],
        attempts=[
            attempt(
                state="pausing",
                state_since=NOW - timedelta(seconds=61),
                pause_reason="memory_breach",
            ),
            attempt(state="cancelling", state_since=NOW - timedelta(seconds=5)),
            attempt(
                state="failed",
                unknown=True,
                lease_expires_at=NOW - timedelta(seconds=1),
                stop_requested_at=NOW - timedelta(seconds=6),
            ),
        ],
    )
    assert result.guard_overdue == {
        "memory_breach": 1,
        "latency_breach": 0,
        "thermal_breach": 0,
        "lease_expired": 1,
        "cancel": 1,
    }
    assert result.recovery_oldest == 500
    assert result.progress_oldest == 0
    cleared = reduce_training_metrics(
        now=NOW, jobs={}, attempts=[], recoveries=[], pending_checkpoints=[]
    )
    assert all(v == 0 for v in cleared.guard_overdue.values())


def test_sdk_snapshot_is_absent_until_success_and_has_closed_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    emitter = TrainingBaselineMetrics(provider.get_meter("test.training"))
    assert reader.get_metrics_data() is None
    snapshot = reduce_training_metrics(
        now=NOW,
        jobs={"running": 1, "private-state": 99},
        attempts=[],
        recoveries=[],
        pending_checkpoints=[],
    )
    emitter.publish(snapshot)
    data = reader.get_metrics_data()
    assert data is not None
    values = {
        m.name: m
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for m in scope.metrics
    }
    assert len(values) == 6
    assert values["coire_training_snapshot_timestamp"].unit == "s"
    timestamp_data = values["coire_training_snapshot_timestamp"].data
    assert isinstance(timestamp_data, Gauge)
    assert timestamp_data.data_points[0].value == NOW.timestamp()
    assert {
        (p.attributes or {})["state"] for p in values["coire_training_jobs"].data.data_points
    } == set(TRAINING_STATES)
    assert {
        (p.attributes or {})["reason"]
        for p in values["coire_training_guard_overdue"].data.data_points
    } == set(GUARD_REASONS)
    assert all(
        set(p.attributes or {}) <= {"state", "reason"}
        for m in values.values()
        for p in m.data.data_points
    )
    provider.shutdown()


async def test_failed_poll_preserves_timestamp_and_does_not_log_sql(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    emitter = TrainingBaselineMetrics(MeterProvider().get_meter("test.failed"))
    snapshot = reduce_training_metrics(
        now=NOW, jobs={}, attempts=[], recoveries=[], pending_checkpoints=[]
    )
    emitter.publish(snapshot)
    stop = asyncio.Event()

    @asynccontextmanager
    async def broken_session() -> AsyncIterator[None]:
        stop.set()
        if stop.is_set():
            raise RuntimeError("private source / secret SQL")
        yield None

    monkeypatch.setattr(training_metrics, "session_scope", broken_session)
    await training_metrics.poll_training_metrics(stop, emitter=emitter)
    assert emitter.snapshot is snapshot
    assert "training baseline refresh failed" in caplog.text
    assert "private source" not in caplog.text


@pytest.mark.parametrize("interval", [0, -1, 16])
async def test_poll_interval_is_bounded(interval: float) -> None:
    with pytest.raises(ValueError):
        await training_metrics.poll_training_metrics(asyncio.Event(), interval_s=interval)
