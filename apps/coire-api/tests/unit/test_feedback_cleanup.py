"""Cleanup is independent of admission and never emits exception contents."""

import asyncio

import pytest

from coire_core.settings import Settings
from coire_scheduler import feedback


async def test_cleanup_worker_runs_with_training_and_preference_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",
        training_enabled=False,
        preference_training_enabled=False,
        diagnostics_enabled=False,
    )  # type: ignore[call-arg]
    worker = feedback.FeedbackCleanupWorker(settings)
    called = asyncio.Event()

    async def cleanup(value: Settings) -> int:
        assert value is settings
        called.set()
        return 0

    monkeypatch.setattr(feedback, "cleanup_feedback", cleanup)
    await worker.start()
    await asyncio.wait_for(called.wait(), timeout=1)
    await worker.stop()
    assert worker.task is None


async def test_cleanup_failure_logs_only_safe_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = feedback.FeedbackCleanupWorker(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]

    async def cleanup(_value: Settings) -> int:
        worker.stop_event.set()
        raise ValueError("private prompt and answer")

    monkeypatch.setattr(feedback, "cleanup_feedback", cleanup)
    await worker.run()
    assert "private" not in caplog.text and "answer" not in caplog.text
    assert caplog.records[-1].__dict__["safe_reason"] == "ValueError"
