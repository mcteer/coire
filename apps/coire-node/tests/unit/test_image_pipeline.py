"""Unsupported native settings fail before a cache hit or denoise call."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from PIL import Image

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    NodeImageInputManifest,
    NodeImageStartRequest,
)
from coire_core.models.images import (
    ImageControl,
    ImageInputDigest,
    ImageLora,
    ImageManifestDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_node.image_dispatch import _supported
from coire_node.image_runtime.cache import NativeStageCache
from coire_node.image_runtime.pipeline import (
    ImagePipelineUnavailable,
    MfluxTxt2ImgPipeline,
    _BoundFluxPromptCache,
)
from coire_node.image_runtime.preflight import RUNTIME_VERSION, ImageCopyUnavailable
from coire_node.store import Store


class _Callbacks:
    def register(self, callback: object) -> None:
        del callback


class _Model:
    def __init__(self) -> None:
        self.callbacks = _Callbacks()
        self.calls = 0

    def generate_image(self, **kwargs: object) -> Image.Image:
        del kwargs
        self.calls += 1
        raise AssertionError("unsupported settings reached the native model")


def _fixture() -> tuple[MfluxTxt2ImgPipeline, _Model, ImageWorkerLoadRequest]:
    request = ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version=RUNTIME_VERSION,
    )
    model = _Model()
    return MfluxTxt2ImgPipeline(cast(Any, model), request), model, request


def test_fill_passes_exact_source_mask_and_guidance_to_native_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, request = _fixture()
    source_id, mask_id = uuid.uuid4(), uuid.uuid4()
    source, mask = tmp_path / "source.png", tmp_path / "mask.png"
    Image.new("RGB", (64, 64), "blue").save(source)
    Image.new("L", (64, 64), 255).save(mask)

    class Callbacks(_Callbacks):
        def register(self, callback: object) -> None:
            model.callback = callback

    class FillModel(_Model):
        def __init__(self) -> None:
            super().__init__()
            self.callback: Any = None
            self.callbacks = Callbacks()

        def generate_image(self, **kwargs: object) -> Image.Image:
            self.calls += 1
            assert kwargs["image_path"] == source
            assert kwargs["masked_image_path"] == mask
            assert kwargs["guidance"] == 4.0
            assert "negative_prompt" not in kwargs
            for step in range(2):
                self.callback.call_in_loop(
                    t=step,
                    seed=7,
                    prompt="fill a square",
                    latents=step,
                    config=None,
                    time_steps=None,
                )
            return Image.new("RGB", (64, 64), "red")

    model = FillModel()
    loaded = MfluxTxt2ImgPipeline(cast(Any, model), request, fill=True)
    monkeypatch.setattr("coire_node.image_runtime.pipeline._sync_latents", lambda value: None)
    spec = ImageSpec(
        model_id=request.model_id,
        mode=ImageMode.FILL,
        prompt="fill a square",
        width=64,
        height=64,
        steps=2,
        guidance=Decimal(4),
        seed=7,
        init_image_id=source_id,
        mask_id=mask_id,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=request.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
        inputs=(
            ImageInputDigest(input_id=source_id, sha256="a" * 64, width=64, height=64),
            ImageInputDigest(input_id=mask_id, sha256="b" * 64, width=64, height=64),
        ),
    )
    images = loaded.generate(
        resolved, lambda *_: None, input_paths={source_id: source, mask_id: mask}
    )
    assert len(images) == 1 and model.calls == 1
    images[0].close()
    with pytest.raises(ImagePipelineUnavailable):
        loaded.generate(resolved, lambda *_: None, input_paths={source_id: source})
    assert model.calls == 1


def test_flux_native_prompt_cache_is_byte_bounded_and_evicts_old_encodings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Encoding:
        nbytes = 12

    monkeypatch.setattr("coire_node.image_runtime.pipeline._sync_encodings", lambda *_: None)
    cache = NativeStageCache(48)
    mapping = _BoundFluxPromptCache(cache, "a" * 64)
    first = (Encoding(), Encoding())
    second = (Encoding(), Encoding())
    third = (Encoding(), Encoding())
    mapping["first"] = first
    mapping["second"] = second
    assert cache.used_bytes == 48
    assert "first" in mapping and mapping["first"] is first
    mapping["third"] = third
    assert cache.used_bytes == 48
    assert "second" not in mapping
    assert "third" in mapping and mapping["third"] is third


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "img2img", "init_image_id": uuid.uuid4(), "strength": "0.375"},
        {"mode": "fill", "init_image_id": uuid.uuid4(), "mask_id": uuid.uuid4()},
        {
            "mode": "control",
            "control": {
                "type": "canny",
                "image_id": uuid.uuid4(),
                "model_id": uuid.uuid4(),
                "strength": "0.125",
                "low_threshold": 100,
                "high_threshold": 200,
            },
        },
        {"loras": [{"model_id": uuid.uuid4(), "scale": "0.125"}]},
        {"upscale": {"model_id": uuid.uuid4(), "factor": 2}},
        {"guidance": "0.125"},
        {"negative_prompt": "not this"},
    ],
)
def test_unsupported_valid_spec_is_refused_without_native_work(change: dict[str, object]) -> None:
    loaded, model, request = _fixture()
    spec = ImageSpec.model_validate(
        {
            "model_id": request.model_id,
            "prompt": "a blue square",
            "width": 64,
            "height": 64,
            "steps": 1,
            "guidance": "0",
            "seed": 1,
            **change,
        }
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=request.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )
    assert isinstance(spec.guidance, Decimal)
    with pytest.raises(ImagePipelineUnavailable):
        loaded.generate(resolved, lambda *_: None)
    assert model.calls == 0
    assert loaded.prompt_cache.occupancy("prompt") == 0


def test_missing_local_model_copy_fails_before_native_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    _, _, request = _fixture()
    with pytest.raises(ImageCopyUnavailable):
        MfluxTxt2ImgPipeline.load(Store(tmp_path), request)


def test_unimplemented_hidden_dependency_fails_before_native_execution() -> None:
    loaded, model, load = _fixture()
    spec = ImageSpec(
        model_id=load.model_id,
        prompt="a blue square",
        width=64,
        height=64,
        steps=1,
        guidance=Decimal(0),
        seed=1,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=load.manifest_sha256,
        dependencies=(
            ImageManifestDigest(
                model_id=uuid.uuid4(),
                slug="org--adapter",
                revision="published",
                sha256="c" * 64,
            ),
        ),
        spec_hash=canonical_spec_hash(spec),
    )
    command = NodeImageStartRequest(
        job_id="01J00000000000000000000000",
        attempt=1,
        fence=1,
        node="coire-edge-b",
        model_id=load.model_id,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        reservation_bytes=load.reservation_bytes,
    )
    assert not _supported(command, load)
    with pytest.raises(ImagePipelineUnavailable):
        loaded.generate(resolved, lambda *_: None)
    assert model.calls == 0


def test_node_accepts_only_exact_lora_dependency_set() -> None:
    _loaded, _model, load = _fixture()
    adapter_id = uuid.uuid4()
    spec = ImageSpec(
        model_id=load.model_id,
        prompt="a blue square",
        width=64,
        height=64,
        steps=1,
        guidance=Decimal(0),
        seed=1,
        loras=(ImageLora(model_id=adapter_id, scale=Decimal("0.375125")),),
    )
    dependency = ImageManifestDigest(
        model_id=adapter_id, slug="org--adapter", revision="a" * 40, sha256="c" * 64
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=load.manifest_sha256,
        dependencies=(dependency,),
        spec_hash=canonical_spec_hash(spec),
    )
    command = NodeImageStartRequest(
        job_id="01J00000000000000000000000",
        attempt=1,
        fence=1,
        node="coire-edge-b",
        model_id=load.model_id,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        reservation_bytes=load.reservation_bytes,
    )
    assert _supported(command, load)
    assert not _supported(
        command.model_copy(update={"resolved": resolved.model_copy(update={"dependencies": ()})}),
        load,
    )


def test_node_accepts_exact_control_asset_and_bound_input() -> None:
    _loaded, _model, load = _fixture()
    control_id, input_id = uuid.uuid4(), uuid.uuid4()
    spec = ImageSpec(
        model_id=load.model_id,
        mode=ImageMode.CONTROL,
        prompt="a blue square",
        width=64,
        height=64,
        steps=1,
        guidance=Decimal(0),
        seed=1,
        control=ImageControl(
            image_id=input_id,
            model_id=control_id,
            strength=Decimal("0.375125"),
            low_threshold=23,
            high_threshold=89,
        ),
    )
    dependency = ImageManifestDigest(
        model_id=control_id, slug="alibaba--union", revision="a" * 40, sha256="c" * 64
    )
    source = NodeImageInputManifest(
        input_id=input_id,
        purpose="control",
        sha256="d" * 64,
        byte_count=100,
        width=64,
        height=64,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=load.manifest_sha256,
        dependencies=(dependency,),
        inputs=(ImageInputDigest(input_id=input_id, sha256="d" * 64, width=64, height=64),),
        spec_hash=canonical_spec_hash(spec),
    )
    command = NodeImageStartRequest(
        job_id="01J00000000000000000000000",
        attempt=1,
        fence=1,
        node="coire-edge-b",
        model_id=load.model_id,
        instance_id=load.instance_id,
        resolved=resolved,
        inputs=(source,),
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        reservation_bytes=load.reservation_bytes,
    )
    assert _supported(command, load)
    assert not _supported(
        command.model_copy(update={"resolved": resolved.model_copy(update={"dependencies": ()})}),
        load,
    )


def test_node_fill_requires_both_exact_input_manifests() -> None:
    _, _, load = _fixture()
    source_id, mask_id = uuid.uuid4(), uuid.uuid4()
    spec = ImageSpec(
        model_id=load.model_id,
        mode=ImageMode.FILL,
        prompt="replace the square",
        width=64,
        height=64,
        steps=2,
        guidance=Decimal(4),
        seed=1,
        init_image_id=source_id,
        mask_id=mask_id,
    )
    inputs = tuple(
        NodeImageInputManifest(
            input_id=input_id,
            purpose=cast(Literal["init", "mask"], purpose),
            sha256="c" * 64,
            byte_count=100,
            width=64,
            height=64,
        )
        for input_id, purpose in ((source_id, "init"), (mask_id, "mask"))
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=load.manifest_sha256,
        inputs=tuple(
            ImageInputDigest(input_id=item.input_id, sha256=item.sha256, width=64, height=64)
            for item in inputs
        ),
        spec_hash=canonical_spec_hash(spec),
    )
    command = NodeImageStartRequest(
        job_id="01J00000000000000000000000",
        attempt=1,
        fence=1,
        node="coire-edge-b",
        model_id=load.model_id,
        instance_id=load.instance_id,
        resolved=resolved,
        inputs=inputs,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        reservation_bytes=load.reservation_bytes,
    )
    assert _supported(command, load)
    assert not _supported(command.model_copy(update={"inputs": inputs[:1]}), load)
    assert not _supported(
        command.model_copy(
            update={
                "inputs": (
                    inputs[0].model_copy(update={"purpose": "mask"}),
                    inputs[1].model_copy(update={"purpose": "init"}),
                )
            }
        ),
        load,
    )
