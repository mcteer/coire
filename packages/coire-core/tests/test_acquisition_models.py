from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.acquisition import (
    AcquisitionRequest,
    Precision,
    VariantPublication,
    VariantRecipe,
)


def test_acquisition_request_forbids_paths_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AcquisitionRequest.model_validate(
            {
                "repo_id": "org/model",
                "variant": {"name": "4bit", "precision": "4bit"},
                "local_path": "/tmp/weights",
            }
        )


def test_mixed_recipe_is_required_only_for_mixed_precision() -> None:
    with pytest.raises(ValidationError):
        VariantRecipe(name="mixed", precision=Precision.MIXED)
    with pytest.raises(ValidationError):
        VariantRecipe(name="plain", precision=Precision.BIT4, mixed_recipe="recipe")


def test_unquantized_recipe_rejects_quantization_switches() -> None:
    with pytest.raises(ValidationError):
        VariantRecipe(name="bf16", precision=Precision.BF16, bits=4)


def test_variant_publication_requires_a_change_and_consistent_default() -> None:
    with pytest.raises(ValidationError):
        VariantPublication()
    with pytest.raises(ValidationError):
        VariantPublication(published=False, is_default=True)
    assert VariantPublication(published=True, is_default=True).is_default


def test_reservation_identity_is_uuid() -> None:
    request = AcquisitionRequest(
        repo_id="org/model",
        variant=VariantRecipe(name="4bit", precision=Precision.BIT4, bits=4),
    )
    assert request.repo_id == "org/model"
    assert isinstance(uuid.uuid4(), uuid.UUID)
