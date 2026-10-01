"""Image acquisition, isolated parser and console shapes remain distinct."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from coire_core.models.acquisition import InspectionResult, NodeValidateRequest, ValidationResult
from coire_core.models.console import ImageActivityItem
from coire_core.models.files import ImageFileProcessRequest, ImageFileProcessResult
from coire_core.models.images import ImageCapabilityProfile, ImageMode

JOB = "01J00000000000000000000000"
MODEL = uuid.UUID("10000000-0000-0000-0000-000000000001")
INPUT = uuid.UUID("20000000-0000-0000-0000-000000000001")


def _capability() -> ImageCapabilityProfile:
    return ImageCapabilityProfile(
        modes=(ImageMode.TXT2IMG,),
        min_width=256,
        max_width=1024,
        min_height=256,
        max_height=1024,
        max_pixels=1024 * 1024,
        min_steps=2,
        max_steps=30,
        min_guidance=Decimal("0"),
        max_guidance=Decimal("4"),
        max_outputs=2,
    )


def _inspection(**overrides: object) -> InspectionResult:
    values: dict[str, object] = {
        "revision": "v1",
        "source_format": "safetensors",
        "supported": True,
        "metadata_bytes": 0,
        "weight_bytes": 0,
        "total_bytes": 0,
    }
    values.update(overrides)
    return InspectionResult.model_validate(values)


def test_legacy_inspection_defaults_and_image_kind_backend() -> None:
    legacy = _inspection()
    assert legacy.kind == "language_model"
    image = _inspection(kind="image_model", backend="mflux")
    assert image.backend == "mflux"
    with pytest.raises(ValidationError, match="backend"):
        _inspection(kind="image_model", backend="mlx_lm")
    with pytest.raises(ValidationError, match="backend"):
        _inspection(kind="image_lora", backend="mflux")


def test_image_validation_requires_measured_profile() -> None:
    values: dict[str, object] = {
        "validator_version": "v1",
        "kind": "image_model",
        "backend": "mflux",
        "smoke": "pass",
        "tolerance": 0.1,
        "perplexity_outcome": "not_applicable",
        "template": "not_applicable",
        "validated": True,
        "created_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError, match="image_capability_profile"):
        ValidationResult.model_validate(values)
    result = ValidationResult.model_validate({**values, "image_capability_profile": _capability()})
    assert result.validated
    with pytest.raises(ValidationError, match="dedicated image command"):
        NodeValidateRequest.model_validate(
            {"job_id": MODEL, "slug": "org--model", "kind": "image_model", "backend": "mflux"}
        )


def test_recipe_parser_is_separate_and_has_64_mib_limit() -> None:
    base: dict[str, object] = {
        "job_id": JOB,
        "input_id": INPUT,
        "source_sha256": "a" * 64,
        "purpose": "recipe",
        "operation": "extract_recipe",
        "byte_count": 64 * 1024 * 1024,
        "deadline_at": datetime.now(UTC),
    }
    ImageFileProcessRequest.model_validate(base)
    for change in (
        {"byte_count": 64 * 1024 * 1024 + 1},
        {"operation": "normalize_image"},
        {"path": "/tmp/recipe"},
    ):
        with pytest.raises(ValidationError):
            ImageFileProcessRequest.model_validate({**base, **change})
    with pytest.raises(ValidationError, match="byte_count"):
        ImageFileProcessRequest.model_validate(
            {
                **base,
                "purpose": "init",
                "operation": "normalize_image",
                "byte_count": 10 * 1024 * 1024 + 1,
                "output_id": uuid.uuid4(),
            }
        )


def test_recipe_result_rejects_metadata_above_64_kib() -> None:
    base: dict[str, object] = {
        "job_id": JOB,
        "input_id": INPUT,
        "operation": "extract_recipe",
        "source_sha256": "a" * 64,
        "recipe_json": "{}",
    }
    ImageFileProcessResult.model_validate(base)
    with pytest.raises(ValidationError, match="recipe_json"):
        ImageFileProcessResult.model_validate({**base, "recipe_json": "é" * 40000})
    with pytest.raises(ValidationError, match="recipe"):
        ImageFileProcessResult.model_validate({**base, "normalized_sha256": "b" * 64})


def test_admin_image_activity_has_ulid_identity_and_no_content() -> None:
    activity = ImageActivityItem(
        job_id=JOB,
        owner_id=uuid.uuid4(),
        model_id=MODEL,
        state="running",
        started_at=datetime.now(UTC),
        can_stop=True,
    )
    assert activity.job_id == JOB
    with pytest.raises(ValidationError, match="job_id"):
        ImageActivityItem.model_validate({**activity.model_dump(), "job_id": str(MODEL)})
    with pytest.raises(ValidationError, match="prompt"):
        ImageActivityItem.model_validate({**activity.model_dump(), "prompt": "private"})
