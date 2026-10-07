"""Training limits tighten safely and expected failures use typed problem details."""

from typing import Any

import pytest
from pydantic import ValidationError

from coire_core.errors import (
    CoireError,
    TrainingConflict,
    TrainingForbidden,
    TrainingNotFound,
    TrainingQuotaExceeded,
    TrainingUnavailable,
    TrainingUploadTooLarge,
    TrainingValidationError,
)
from coire_core.settings import Settings


def test_training_defaults_are_off_and_deadlines_are_bounded() -> None:
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert not settings.training_enabled
    assert settings.training_execution_lease_s == 30
    assert settings.training_lease_renew_s < settings.training_execution_lease_s
    assert settings.training_cancel_grace_s == 5
    assert settings.training_pause_grace_s == 60
    assert settings.training_latency_min_samples == 30


@pytest.mark.parametrize(
    "override",
    [
        {"training_cancel_grace_s": 6},
        {"training_pause_grace_s": 61},
        {"training_recipe_max_bytes": 65537},
        {"training_dataset_upload_max_bytes": 257 * 1024**2},
        {"training_latency_min_samples": 29},
        {"training_measurement_min_requests": 99},
        {"training_execution_lease_s": 5, "training_lease_renew_s": 5},
        {"training_max_pending_global": 2, "training_max_pending_per_admin": 3},
        {"training_list_page_default": 25, "training_list_page_max": 10},
    ],
)
def test_training_config_cannot_weaken_protection(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir="/nonexistent", **override)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "error,status",
    [
        (TrainingNotFound, 404),
        (TrainingForbidden, 403),
        (TrainingConflict, 409),
        (TrainingValidationError, 422),
        (TrainingUploadTooLarge, 413),
        (TrainingQuotaExceeded, 429),
        (TrainingUnavailable, 503),
    ],
)
def test_training_errors_have_safe_rfc9457_projection(error: type[CoireError], status: int) -> None:
    problem = error().to_problem()
    assert problem.status == status
    assert problem.type.startswith("urn:coire:training_")
