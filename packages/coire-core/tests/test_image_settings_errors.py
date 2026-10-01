"""Image admission stays off and resource ceilings are not configurable upward."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coire_core.errors import (
    CoireError,
    ImageConflict,
    ImageForbidden,
    ImageInputTooLarge,
    ImageNotFound,
    ImageQuotaExceeded,
    ImageStorageUnavailable,
    ImageUnsupportedInput,
    ImageValidationError,
)
from coire_core.settings import Settings


def _settings(**values: object) -> Settings:
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    for name, value in values.items():
        setattr(settings, name, value)
    return settings


def test_image_defaults_are_disabled_and_bounded() -> None:
    settings = _settings()
    assert settings.image_enabled is False
    assert settings.image_generation_input_max_bytes == 10 * 1024**2
    assert settings.image_recipe_input_max_bytes == 64 * 1024**2
    assert settings.image_recipe_metadata_max_bytes == 64 * 1024
    assert settings.image_output_max_bytes == 64 * 1024**2
    assert settings.image_max_outputs == 4
    assert settings.image_pending_per_owner == 4
    assert settings.image_pending_global == 32
    assert settings.image_daily_outputs_per_owner == 100
    assert settings.image_worker_idle_ttl_s == 900


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_generation_input_max_bytes", 10 * 1024**2 + 1),
        ("image_recipe_input_max_bytes", 64 * 1024**2 + 1),
        ("image_recipe_metadata_max_bytes", 64 * 1024 + 1),
        ("image_output_max_bytes", 64 * 1024**2 + 1),
        ("image_max_outputs", 5),
        ("image_pending_per_owner", 5),
        ("image_pending_global", 33),
        ("image_daily_outputs_per_owner", 101),
        ("image_worker_idle_ttl_s", 86_401),
    ],
)
def test_image_upper_bounds_cannot_be_raised(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        _settings(**{field: value})


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ImageNotFound, 404, "image_not_found"),
        (ImageForbidden, 403, "image_forbidden"),
        (ImageConflict, 409, "image_conflict"),
        (ImageValidationError, 422, "image_validation_error"),
        (ImageInputTooLarge, 413, "image_input_too_large"),
        (ImageUnsupportedInput, 415, "image_unsupported_input"),
        (ImageQuotaExceeded, 429, "image_quota_exceeded"),
        (ImageStorageUnavailable, 507, "image_storage_unavailable"),
    ],
)
def test_image_errors_have_safe_problem_details(
    error: type[CoireError], status: int, code: str
) -> None:
    problem = error().to_problem()
    assert problem.status == status
    assert problem.coire_code == code
    assert "traceback" not in (problem.detail or "").lower()
