"""Registry image kinds cannot be mistaken for chat engines."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from coire_core.models.engine import EngineStartRequest
from coire_core.models.images import ImageCapabilityProfile, ImageMode
from coire_core.models.registry import EngineBackend, Model, ModelKind, ModelListing

MODEL_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")


def _model(**overrides: object) -> Model:
    values: dict[str, object] = {
        "id": MODEL_ID,
        "repo_id": "org/model",
        "slug": "org--model",
        "display_name": "Model",
        "state": "ready",
        "placement_policy": "single:auto",
        "precision": "4bit",
        "weight_bytes": 1,
        "total_bytes": 1,
        "file_count": 1,
        "memory_estimate_bytes": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Model.model_validate(values)


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


def test_legacy_registry_default_stays_language_model() -> None:
    old = _model()
    assert old.kind is ModelKind.LANGUAGE_MODEL
    assert old.backend is EngineBackend.MLX_LM
    assert old.image_capability_profile is None


def test_generation_base_requires_mflux_and_measured_profile_when_ready() -> None:
    image = _model(kind="image_model", backend="mflux", image_capability_profile=_capability())
    assert image.kind is ModelKind.IMAGE_MODEL
    with pytest.raises(ValidationError, match="backend"):
        _model(kind="image_model", image_capability_profile=_capability())
    with pytest.raises(ValidationError, match="image_capability_profile"):
        _model(kind="image_model", backend="mflux")
    with pytest.raises(ValidationError, match="image worker contract"):
        EngineStartRequest.model_validate(
            {"engine_id": MODEL_ID, "slug": "org--model", "backend": "mflux", "estimate_bytes": 1}
        )


@pytest.mark.parametrize(
    "kind", ["image_lora", "control_model", "upscale_model", "image_classifier"]
)
def test_auxiliary_kinds_are_not_routable_engines(kind: str) -> None:
    binding = (
        {"compatible_base_model_id": str(MODEL_ID)}
        if kind in {"image_lora", "control_model"}
        else {}
    )
    auxiliary = _model(kind=kind, backend="auxiliary", capability_profile=binding)
    assert auxiliary.backend is EngineBackend.AUXILIARY
    if binding:
        assert auxiliary.capability_profile.compatible_base_model_id == MODEL_ID
    with pytest.raises(ValidationError, match="backend"):
        _model(kind=kind, backend="mlx_lm")
    with pytest.raises(ValidationError, match="auxiliary"):
        EngineStartRequest.model_validate(
            {
                "engine_id": MODEL_ID,
                "slug": "org--model",
                "backend": "auxiliary",
                "estimate_bytes": 1,
            }
        )


def test_auxiliary_base_binding_is_kind_scoped() -> None:
    for kind in ("image_lora", "control_model"):
        with pytest.raises(ValidationError, match="compatible base"):
            _model(kind=kind, backend="auxiliary")
    for kind, backend, profile in (
        ("language_model", "mlx_lm", {}),
        ("image_model", "mflux", {"image_capability_profile": _capability()}),
        ("image_classifier", "auxiliary", {}),
        ("upscale_model", "auxiliary", {}),
    ):
        with pytest.raises(ValidationError, match="bind a base model"):
            _model(
                kind=kind,
                backend=backend,
                capability_profile={"compatible_base_model_id": str(MODEL_ID)},
                **profile,
            )


def test_chat_listing_refuses_image_kind_even_if_service_selects_it() -> None:
    listing: dict[str, object] = {
        "id": MODEL_ID,
        "display_name": "Model",
        "precision": "4bit",
        "load_state": "cold",
        "capability_profile": {},
    }
    ModelListing.model_validate(listing)
    with pytest.raises(ValidationError, match="chat listing"):
        ModelListing.model_validate({**listing, "kind": "image_model", "backend": "mflux"})
    with pytest.raises(ValidationError, match="chat listing"):
        ModelListing.model_validate({**listing, "kind": "image_classifier", "backend": "auxiliary"})
