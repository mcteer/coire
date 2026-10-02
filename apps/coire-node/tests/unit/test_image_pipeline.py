"""Unsupported native settings fail before a cache hit or denoise call."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest, NodeImageStartRequest
from coire_core.models.images import (
    ImageLora,
    ImageManifestDigest,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_node.image_dispatch import _supported
from coire_node.image_runtime.pipeline import ImagePipelineUnavailable, MfluxTxt2ImgPipeline
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
