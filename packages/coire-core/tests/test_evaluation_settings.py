"""Evaluation admission is opt-in and cannot increase active concurrency."""

import pytest
from pydantic import ValidationError

from coire_core.settings import Settings


def test_evaluation_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVALUATIONS_ENABLED", raising=False)
    settings = Settings()
    assert not settings.evaluations_enabled
    assert settings.evaluation_max_active_groups == 1
    assert settings.evaluation_max_pending_runs == 100
    assert settings.evaluation_queue_timeout_seconds == 3600
    assert settings.evaluation_timeout_seconds == 900
    assert settings.evaluation_evidence_retention_days == 7
    assert settings.evaluation_evidence_quota_bytes == 1024**3


@pytest.mark.parametrize(
    "field,value",
    [
        ("evaluation_max_active_groups", 2),
        ("evaluation_max_pending_runs", 101),
        ("evaluation_queue_timeout_seconds", 59),
        ("evaluation_timeout_seconds", 1801),
        ("evaluation_evidence_retention_days", 31),
        ("evaluation_evidence_quota_bytes", 1),
    ],
)
def test_evaluation_limits_are_enforced(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


def test_runtime_environment_enables_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALUATIONS_ENABLED", "true")
    assert Settings().evaluations_enabled
