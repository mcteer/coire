"""Unsupported native settings fail before a cache hit or denoise call."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import ImageSpec, ResolvedImageSpec, canonical_spec_hash
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
