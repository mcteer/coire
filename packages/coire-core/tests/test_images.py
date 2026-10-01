"""Versioned image contracts refuse unsupported work before admission."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentTag,
    ImageInput,
    ImageInputDigest,
    ImageInputUpload,
    ImageJob,
    ImageJobEvent,
    ImageJobState,
    ImageLora,
    ImageManifestDigest,
    ImageMode,
    ImageOutput,
    ImagePresetCreate,
    ImageRecipe,
    ImageRecipeImport,
    ImageRecipeImportRequest,
    ImageSpec,
    ImageSubmitRequest,
    OpenAIImageData,
    OpenAIImageGenerationRequest,
    ResolvedImageSpec,
    canonical_client_intent_hash,
    canonical_recipe_bytes,
    canonical_spec_hash,
    expand_image_seeds,
    pixel_digest,
)

MODEL = uuid.UUID("10000000-0000-0000-0000-000000000001")
INPUT = uuid.UUID("20000000-0000-0000-0000-000000000001")


def spec(**overrides: object) -> ImageSpec:
    values: dict[str, object] = {
        "model_id": MODEL,
        "mode": "txt2img",
        "prompt": "a red fox",
        "width": 512,
        "height": 512,
        "steps": 20,
        "guidance": "3.125",
        "n": 1,
    }
    values.update(overrides)
    return ImageSpec.model_validate(values)


def test_submit_is_root_level_and_forbids_untrusted_fields() -> None:
    request = ImageSubmitRequest.model_validate({"model_id": MODEL, "prompt": "fox"})
    assert request.model_id == MODEL
    for field in ({"spec": {"prompt": "fox"}}, {"path": "/tmp/x"}, {"is_admin": True}):
        with pytest.raises(ValidationError):
            ImageSubmitRequest.model_validate(field)


@pytest.mark.parametrize(
    ("overrides", "offending"),
    [
        ({"mode": "img2img"}, "init_image_id"),
        ({"mode": "fill", "init_image_id": INPUT}, "mask_id"),
        ({"mode": "control"}, "control"),
        ({"mode": "txt2img", "init_image_id": INPUT}, "init_image_id"),
        ({"mode": "img2img", "init_image_id": INPUT, "strength": 0}, "strength"),
        ({"width": 4097}, "width"),
        ({"height": 513}, "height"),
        ({"n": 5}, "n"),
        ({"seed": 2**32}, "seed"),
        ({"guidance": "NaN"}, "guidance"),
        ({"output": {"format": "jpeg"}}, "format"),
        ({"loras": [{"model_id": MODEL, "scale": "1.25"}] * 2}, "loras"),
    ],
)
def test_spec_rejects_invalid_modes_and_bounds(
    overrides: dict[str, object], offending: str
) -> None:
    with pytest.raises(ValidationError, match=offending):
        spec(**overrides)


def test_exact_lora_scale_and_seed_wrap() -> None:
    image = spec(loras=[ImageLora(model_id=MODEL, scale=Decimal("0.123456789123456789"))])
    assert image.loras[0].scale == Decimal("0.123456789123456789")
    assert expand_image_seeds(2**32 - 1, 3) == [2**32 - 1, 0, 1]


def test_upload_caps_are_purpose_specific() -> None:
    ImageInputUpload(purpose="recipe", filename="old.png", byte_count=64 * 1024 * 1024)
    ImageInputUpload(purpose="init", filename="source.jpg", byte_count=1024)
    with pytest.raises(ValidationError, match="recipe filename"):
        ImageInputUpload(purpose="recipe", filename="old.jpg", byte_count=1024)
    with pytest.raises(ValidationError, match="byte_count"):
        ImageInputUpload(purpose="init", filename="source.png", byte_count=10 * 1024 * 1024 + 1)
    with pytest.raises(ValidationError, match="byte_count"):
        ImageInputUpload(purpose="recipe", filename="old.png", byte_count=64 * 1024 * 1024 + 1)
    with pytest.raises(ValidationError, match="filename"):
        ImageInputUpload(purpose="recipe", filename="../old.png", byte_count=1)
    with pytest.raises(ValidationError, match="filename"):
        ImageInputUpload(purpose="recipe", filename="bad\x00.png", byte_count=1)


def test_recipe_input_is_not_a_generation_source() -> None:
    base: dict[str, object] = {
        "id": INPUT,
        "purpose": "recipe",
        "state": "ready",
        "byte_count": 20 * 1024 * 1024,
        "sha256": "a" * 64,
        "created_at": datetime.now(UTC),
    }
    ImageInput.model_validate(base)
    with pytest.raises(ValidationError, match="recipe input"):
        ImageInput.model_validate({**base, "width": 512})
    with pytest.raises(ValidationError, match="byte_count"):
        ImageInput.model_validate({**base, "purpose": "init"})
    with pytest.raises(ValidationError, match="replacement_inputs"):
        ImageRecipeImportRequest.model_validate({"replacement_inputs": {"/etc/passwd": INPUT}})


def test_client_intent_hash_ignores_field_order_but_not_values() -> None:
    first = ImageSubmitRequest.model_validate(
        {"prompt": "fox", "model_id": MODEL, "guidance": "1.234567890123456789"}
    )
    second = ImageSubmitRequest.model_validate(
        {"guidance": "1.234567890123456789", "model_id": MODEL, "prompt": "fox"}
    )
    changed = ImageSubmitRequest.model_validate(
        {"prompt": "fox!", "model_id": MODEL, "guidance": "1.234567890123456789"}
    )
    assert canonical_client_intent_hash(first) == canonical_client_intent_hash(second)
    assert canonical_client_intent_hash(first) != canonical_client_intent_hash(changed)


def test_recipe_is_bounded_precise_and_has_separate_pixel_digest() -> None:
    resolved_spec = spec(seed=4, guidance="1.234567890123456789")
    resolved = ResolvedImageSpec(
        spec=resolved_spec,
        seeds=(4,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(resolved_spec),
    )
    recipe = ImageRecipe(
        resolved=resolved, output_index=0, seed=4, pixel_sha256="d" * 64, width=512, height=512
    )
    encoded = canonical_recipe_bytes(recipe)
    assert b"1.234567890123456789" in encoded
    assert ImageRecipe.model_validate_json(encoded) == recipe
    assert pixel_digest(b"pixels", width=2, height=1, channels=3) != pixel_digest(
        b"pixelz", width=2, height=1, channels=3
    )
    with pytest.raises(ValidationError):
        ImageRecipe.model_validate({**recipe.model_dump(), "owner_id": str(MODEL)})
    with pytest.raises(ValidationError, match="spec_hash"):
        ResolvedImageSpec.model_validate({**resolved.model_dump(), "spec_hash": "0" * 64})
    with pytest.raises(ValidationError, match="output dimensions"):
        ImageRecipe.model_validate({**recipe.model_dump(), "width": 513})
    restored = ImageRecipeImport(
        recipe=recipe,
        settings=ImageSubmitRequest(model_id=MODEL, prompt="fox"),
        exact_reproduction_available=False,
        unavailable_reason="changed runtime",
    )
    assert restored.unavailable_reason == "changed runtime"
    with pytest.raises(ValidationError, match="exact reproduction"):
        ImageRecipeImport(
            recipe=recipe,
            settings=restored.settings,
            exact_reproduction_available=True,
            unavailable_reason="changed runtime",
        )
    with pytest.raises(ValidationError, match="dependencies"):
        ResolvedImageSpec.model_validate(
            {
                **resolved.model_dump(),
                "dependencies": [
                    {"model_id": MODEL, "revision": "v1", "sha256": "a" * 64, "path": "/tmp/x"}
                ],
            }
        )
    assert resolved.seeds == (4,)
    assert resolved.dependencies == ()
    bound = ResolvedImageSpec(
        spec=resolved_spec,
        seeds=(4,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(resolved_spec),
        dependencies=(ImageManifestDigest(model_id=MODEL, revision="v1", sha256="a" * 64),),
        inputs=(ImageInputDigest(input_id=INPUT, sha256="b" * 64, width=512, height=512),),
    )
    assert len(bound.inputs) == 1


def test_capability_limits() -> None:
    capability = ImageCapabilityProfile(
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
    capability.validate_spec(spec())
    with_defaults = ImageCapabilityProfile.model_validate(
        {
            **capability.model_dump(),
            "default_width": 512,
            "default_height": 512,
            "default_steps": 9,
            "default_guidance": "1.5",
        }
    )
    assert with_defaults.default_steps == 9
    with pytest.raises(ValidationError, match="defaults"):
        ImageCapabilityProfile.model_validate({**capability.model_dump(), "default_steps": 9})
    with pytest.raises(ValidationError, match="default_width"):
        ImageCapabilityProfile.model_validate({**with_defaults.model_dump(), "default_width": 2048})
    with pytest.raises(ValueError, match="negative_prompt"):
        capability.validate_spec(spec(negative_prompt="no blur"))
    with pytest.raises(ValueError, match="steps"):
        capability.validate_spec(spec(steps=31))

    dependency = uuid.uuid4()
    profile = ImageCapabilityProfile.model_validate(
        {**capability.model_dump(), "required_dependency_ids": [str(dependency)]}
    )
    assert profile.required_dependency_ids == (dependency,)
    with pytest.raises(ValidationError, match="dependencies"):
        ImageCapabilityProfile.model_validate(
            {**capability.model_dump(), "required_dependency_ids": [str(dependency)] * 2}
        )
    with pytest.raises(ValidationError):
        ImageCapabilityProfile.model_validate(
            {
                **capability.model_dump(),
                "required_dependency_ids": [str(uuid.uuid4()) for _ in range(17)],
            }
        )


def test_events_enforce_job_identity_and_terminal_shapes() -> None:
    effective = spec(seed=1)
    resolved = ResolvedImageSpec(
        spec=effective,
        seeds=(1,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(effective),
    )
    recipe = ImageRecipe(
        resolved=resolved,
        output_index=0,
        seed=1,
        pixel_sha256="c" * 64,
        width=512,
        height=512,
    )
    output = ImageOutput(
        id=uuid.uuid4(),
        job_id="01J00000000000000000000000",
        index=0,
        recipe=recipe,
        tag=ImageContentTag.UNKNOWN,
        byte_count=12,
        file_sha256="d" * 64,
        created_at=datetime.now(UTC),
    )
    event: dict[str, object] = {
        "job_id": "01J00000000000000000000000",
        "sequence": 1,
        "at": datetime.now(UTC),
        "type": "done",
        "state": "succeeded",
        "outputs": [output.model_dump(mode="json")],
    }
    ImageJobEvent.model_validate(event)
    with pytest.raises(ValidationError, match="done requires"):
        ImageJobEvent.model_validate({**event, "outputs": []})
    with pytest.raises(ValidationError, match="job_id"):
        ImageJobEvent.model_validate({**event, "job_id": str(MODEL)})
    with pytest.raises(ValidationError, match="event type and state"):
        ImageJobEvent.model_validate({**event, "state": "failed"})
    with pytest.raises(ValidationError, match="progress requires"):
        ImageJobEvent.model_validate({**event, "type": "progress", "state": "running"})
    snapshot = ImageJob(
        id="01J00000000000000000000000",
        state=ImageJobState.QUEUED,
        effective_spec=spec(),
        resolved=None,
        latest_event_sequence=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    reset_event = {key: value for key, value in event.items() if key != "outputs"}
    ImageJobEvent.model_validate(
        {**reset_event, "type": "reset", "state": "queued", "snapshot": snapshot}
    )
    with pytest.raises(ValidationError, match="reset requires"):
        ImageJobEvent.model_validate({**reset_event, "type": "reset", "state": "queued"})
    with pytest.raises(ValidationError, match="only reset"):
        ImageJobEvent.model_validate({**event, "snapshot": snapshot})


def test_job_exposes_published_outputs_only_after_success() -> None:
    effective = spec(seed=4)
    resolved = ResolvedImageSpec(
        spec=effective,
        seeds=(4,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(effective),
    )
    recipe = ImageRecipe(
        resolved=resolved,
        output_index=0,
        seed=4,
        pixel_sha256="c" * 64,
        width=512,
        height=512,
    )
    output = ImageOutput(
        id=uuid.uuid4(),
        job_id="01J00000000000000000000000",
        index=0,
        recipe=recipe,
        tag=ImageContentTag.NORMAL,
        byte_count=12,
        file_sha256="d" * 64,
        created_at=datetime.now(UTC),
    )
    completed = ImageJob(
        id=output.job_id,
        state=ImageJobState.SUCCEEDED,
        effective_spec=effective,
        resolved=resolved,
        outputs=[output],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert completed.outputs == [output]
    with pytest.raises(ValidationError, match="only succeeded"):
        ImageJob.model_validate({**completed.model_dump(), "state": "failed"})


def test_compatible_format_and_preset_are_strict() -> None:
    OpenAIImageGenerationRequest(model=MODEL, prompt="fox", output_format="png")
    with pytest.raises(ValidationError, match="output_format"):
        OpenAIImageGenerationRequest.model_validate(
            {"model": MODEL, "prompt": "fox", "output_format": "jpeg"}
        )
    with pytest.raises(ValidationError, match="size"):
        OpenAIImageGenerationRequest(model=MODEL, prompt="fox", size="9999x9999")
    with pytest.raises(ValidationError, match="exactly one"):
        OpenAIImageData(url="/image", b64_json="base64")
    with pytest.raises(ValidationError, match="preset defaults"):
        ImagePresetCreate(
            name="bad",
            defaults=ImageSubmitRequest(preset_id=MODEL, prompt="fox"),
        )
