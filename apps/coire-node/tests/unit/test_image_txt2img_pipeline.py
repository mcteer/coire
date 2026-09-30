"""Fixed Turbo execution never silently discards settings or races progress."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import ImageSpec, ResolvedImageSpec, canonical_spec_hash
from coire_node.image_runtime import pipeline
from coire_node.store import Store


class FakeCallbacks:
    def __init__(self) -> None:
        self.callback: Any = None

    def register(self, callback: Any) -> None:
        self.callback = callback


class FakeModel:
    def __init__(self) -> None:
        self.callbacks = FakeCallbacks()
        self.calls: list[dict[str, object]] = []

    def generate_image(self, **kwargs: object) -> Image.Image:
        self.calls.append(kwargs)
        steps = kwargs["num_inference_steps"]
        width = kwargs["width"]
        height = kwargs["height"]
        assert isinstance(steps, int)
        assert isinstance(width, int)
        assert isinstance(height, int)
        for index in range(steps):
            assert self.callbacks.callback is not None
            self.callbacks.callback.call_in_loop(
                t=index,
                seed=kwargs["seed"],
                prompt=kwargs["prompt"],
                latents=index,
                config=None,
                time_steps=None,
            )
        return Image.new("RGB", (width, height))


def _request() -> ImageWorkerLoadRequest:
    return ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )


def _resolved(request: ImageWorkerLoadRequest, **changes: object) -> ResolvedImageSpec:
    values: dict[str, object] = {
        "model_id": request.model_id,
        "prompt": "private prompt",
        "width": 64,
        "height": 64,
        "steps": 2,
        "guidance": Decimal(0),
        "seed": 7,
        "n": 2,
    }
    values.update(changes)
    spec = ImageSpec.model_validate(values)
    return ResolvedImageSpec(
        spec=spec,
        seeds=(7, 8),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="b" * 64,
        model_sha256=request.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )


def _loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: ImageWorkerLoadRequest
) -> tuple[pipeline.MfluxTxt2ImgPipeline, FakeModel]:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    model = FakeModel()
    monkeypatch.setattr(pipeline, "verify_image_copy", lambda store, load: tmp_path)
    monkeypatch.setattr(pipeline, "_load_native", lambda path: model)
    return pipeline.MfluxTxt2ImgPipeline.load(Store(tmp_path), request), model


def test_load_requires_offline_environment_before_native_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = _request()
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setattr(pipeline, "verify_image_copy", lambda store, load: tmp_path)
    monkeypatch.setattr(pipeline, "_load_native", lambda path: pytest.fail("native loader ran"))
    with pytest.raises(pipeline.ImagePipelineUnavailable):
        pipeline.MfluxTxt2ImgPipeline.load(Store(tmp_path), request)


def test_each_seed_has_synchronized_content_free_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = _request()
    loaded, model = _loaded(monkeypatch, tmp_path, request)
    order: list[tuple[str, int]] = []
    monkeypatch.setattr(pipeline, "_sync_latents", lambda value: order.append(("sync", value)))
    result = loaded.generate(
        _resolved(request), lambda index, step, total: order.append(("step", step))
    )
    assert len(result) == 2
    assert all(image.mode == "RGB" and image.size == (64, 64) for image in result)
    assert [call["seed"] for call in model.calls] == [7, 8]
    assert all(call["guidance"] == 0.0 and call["negative_prompt"] is None for call in model.calls)
    assert order == [
        ("sync", 0),
        ("step", 1),
        ("sync", 1),
        ("step", 2),
        ("sync", 0),
        ("step", 1),
        ("sync", 1),
        ("step", 2),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"guidance": Decimal("1")},
        {"negative_prompt": "ignored"},
        {"upscale": {"model_id": uuid.uuid4(), "factor": 2}},
        {"loras": [{"model_id": uuid.uuid4(), "scale": Decimal("1")}]},
    ],
)
def test_unsupported_turbo_settings_refused_before_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changes: dict[str, object]
) -> None:
    request = _request()
    loaded, model = _loaded(monkeypatch, tmp_path, request)
    with pytest.raises(pipeline.ImagePipelineUnavailable):
        loaded.generate(_resolved(request, **changes), lambda index, step, total: None)
    assert model.calls == []


def test_mismatched_model_copy_refused_before_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = _request()
    loaded, model = _loaded(monkeypatch, tmp_path, request)
    resolved = _resolved(request).model_copy(update={"model_sha256": "c" * 64})
    with pytest.raises(pipeline.ImagePipelineUnavailable):
        loaded.generate(resolved, lambda index, step, total: None)
    assert model.calls == []


def test_wrong_output_dimensions_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = _request()
    loaded, model = _loaded(monkeypatch, tmp_path, request)
    monkeypatch.setattr(pipeline, "_sync_latents", lambda value: None)
    original = model.generate_image

    def wrong_size(**kwargs: object) -> Image.Image:
        original(**kwargs).close()
        return Image.new("RGB", (32, 32))

    monkeypatch.setattr(model, "generate_image", wrong_size)
    with pytest.raises(pipeline.ImagePipelineUnavailable):
        loaded.generate(_resolved(request), lambda index, step, total: None)


def test_native_loader_receives_only_verified_local_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = _request()
    seen: list[Path] = []
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(pipeline, "verify_image_copy", lambda store, load: tmp_path)

    def fake_load(path: Path) -> FakeModel:
        seen.append(path)
        return FakeModel()

    monkeypatch.setattr(pipeline, "_load_native", fake_load)
    pipeline.MfluxTxt2ImgPipeline.load(Store(tmp_path), request)
    assert seen == [tmp_path]
