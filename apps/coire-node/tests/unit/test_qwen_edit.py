"""Photo editing uses reference-aware native execution, not Canny or denoising strength."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image, ImageDraw

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import (
    ImageInputDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_node.image_runtime import pipeline


def test_native_edit_preserves_reference_binding_guidance_progress_and_exact_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = ImageWorkerLoadRequest(
        slug="mflux-community--qwen-image-edit-2509-mflux-q8",
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    source_id = uuid.uuid4()
    source = tmp_path / "reference.png"
    Image.new("RGB", (800, 600), "blue").save(source)
    callbacks = SimpleNamespace(callback=None)
    callbacks.register = lambda value: setattr(callbacks, "callback", value)
    calls = []

    class Native:
        def __init__(self) -> None:
            self.callbacks = callbacks

        def generate_image(self, **kwargs: Any) -> Image.Image:
            calls.append(kwargs)
            assert kwargs["image_paths"] == [str(source)]
            assert kwargs["guidance"] == 4.0
            assert "image_strength" not in kwargs and "masked_image_path" not in kwargs
            for step in range(kwargs["num_inference_steps"]):
                callbacks.callback.call_in_loop(
                    t=step,
                    seed=kwargs["seed"],
                    prompt=kwargs["prompt"],
                    latents=step,
                    config=None,
                    time_steps=None,
                )
            return Image.new("RGB", (kwargs["width"], kwargs["height"]), "red")

    monkeypatch.setattr(pipeline, "_sync_latents", lambda _: None)
    loaded = pipeline.MfluxTxt2ImgPipeline(cast(Any, Native()), request, edit=True)
    spec = ImageSpec(
        model_id=request.model_id,
        mode=ImageMode.EDIT,
        prompt="Change the vase to red",
        width=1980,
        height=1113,
        steps=4,
        guidance=Decimal(4),
        seed=7,
        init_image_id=source_id,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version=request.runtime_version,
        environment_fingerprint="b" * 64,
        model_sha256=request.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
        inputs=(ImageInputDigest(input_id=source_id, sha256="c" * 64, width=800, height=600),),
    )
    progress = []
    images = loaded.generate(
        resolved, lambda i, s, t: progress.append((i, s, t)), input_paths={source_id: source}
    )
    assert images[0].size == (1980, 1113)
    assert (calls[0]["width"], calls[0]["height"]) == (1984, 1120)
    assert progress == [(0, i, 4) for i in range(1, 5)]
    assert loaded.cache_status is None
    images[0].close()
    with pytest.raises(pipeline.ImagePipelineUnavailable):
        loaded.generate(resolved, lambda *_: None, input_paths={uuid.uuid4(): source})


def test_qwen_qualification_covers_quality_recipe_and_all_size_orientations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_core.models.image_worker import ImageAssetValidateRequest
    from coire_core.models.registry import ModelKind
    from coire_node import image_validation
    from coire_node.store import Store

    model_id = uuid.uuid4()
    request = ImageAssetValidateRequest(
        job_id=uuid.uuid4(),
        model_id=model_id,
        slug="mflux-community--qwen-image-edit-2509-mflux-q8",
        kind=ModelKind.IMAGE_MODEL,
        source_revision="b" * 40,
        manifest_sha256="a" * 64,
        reservation_id=uuid.uuid4(),
    )
    load = ImageWorkerLoadRequest(
        slug=request.slug,
        model_id=model_id,
        instance_id=request.job_id,
        manifest_sha256=request.manifest_sha256,
        reservation_bytes=10**9,
        runtime_version="mflux-0.20.0",
    )
    calls = []

    class Fake:
        def generate(
            self, resolved: ResolvedImageSpec, progress: Any, *, input_paths: Any
        ) -> tuple[Image.Image, ...]:
            assert resolved.spec.mode is ImageMode.EDIT
            assert (resolved.inputs[0].width, resolved.inputs[0].height) == (800, 600)
            assert resolved.spec.strength is None and input_paths
            calls.append((resolved.spec.width, resolved.spec.height, resolved.spec.steps))
            image = Image.new("RGB", (resolved.spec.width, resolved.spec.height), "white")
            ImageDraw.Draw(image).rectangle((10, 10, 200, 200), fill="red")
            return (image,)

    monkeypatch.setattr(pipeline.MfluxTxt2ImgPipeline, "load", lambda *args: Fake())
    monkeypatch.setattr(image_validation, "resident_bytes", lambda _: 1000)
    result = image_validation._validate_qwen_edit(
        Store(tmp_path), load, request, reservation_bytes=10**9
    )
    assert calls == [(1024, 1024, 40), (1980, 1980, 4), (1980, 1113, 4), (1113, 1980, 4)]
    assert result.validator_version == "qwen-edit-v1"
    assert result.image_capability_profile is not None
    assert result.image_capability_profile.modes == (ImageMode.EDIT,)
    assert result.image_capability_profile.default_steps == 40
    assert len(result.resolution_samples) == 4
