"""Prompt-stage cache hooks stay free of mflux and do not replace denoising."""

from __future__ import annotations

import ast
import inspect
import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import (
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
    expand_image_seeds,
)
from coire_node.image_runtime import pipeline
from coire_node.image_runtime.cache import NativeStageCache, StageCacheKey, stage_identity
from coire_node.image_runtime.preflight import RUNTIME_VERSION


class _Callbacks:
    def __init__(self) -> None:
        self.callback: Any = None

    def register(self, callback: Any) -> None:
        self.callback = callback


class _Model:
    def __init__(self) -> None:
        self.callbacks = _Callbacks()
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
        runtime_version=RUNTIME_VERSION,
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
        "n": 1,
    }
    values.update(changes)
    spec = ImageSpec.model_validate(values)
    assert spec.seed is not None
    return ResolvedImageSpec(
        spec=spec,
        seeds=tuple(expand_image_seeds(spec.seed, spec.n)),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="b" * 64,
        model_sha256=request.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )


def _pipeline(
    *, max_bytes: int | None = None
) -> tuple[pipeline.MfluxTxt2ImgPipeline, _Model, ImageWorkerLoadRequest]:
    request = _request()
    model = _Model()
    if max_bytes is None:
        loaded = pipeline.MfluxTxt2ImgPipeline(cast(Any, model), request)
    else:
        loaded = pipeline.MfluxTxt2ImgPipeline(
            cast(Any, model), request, prompt_cache_max_bytes=max_bytes
        )
    return loaded, model, request


def _parts(
    request: ImageWorkerLoadRequest, resolved: ResolvedImageSpec, seed: int
) -> tuple[str, ...]:
    spec = resolved.spec
    return (
        request.runtime_version,
        request.manifest_sha256,
        spec.prompt,
        str(spec.width),
        str(spec.height),
        str(spec.steps),
        str(seed),
    )


def _key(request: ImageWorkerLoadRequest, resolved: ResolvedImageSpec, seed: int) -> StageCacheKey:
    return StageCacheKey(stage="prompt", identity=stage_identity(*_parts(request, resolved, seed)))


def test_pipeline_module_does_not_import_mflux() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(pipeline))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".", 1)[0])
    assert "mflux" not in imported


def test_prompt_cache_defaults_to_256_mib_and_can_be_overridden() -> None:
    loaded, _model, _request_value = _pipeline()
    assert loaded.prompt_cache.max_bytes == 256 * 1024**2
    assert loaded.adapter_id is None
    smaller, _smaller_model, _smaller_request = _pipeline(max_bytes=4096)
    assert smaller.prompt_cache.max_bytes == 4096
    assert smaller.encoder_cache.max_bytes == 4096


def test_native_encoder_cache_is_byte_bound_and_evicts_lru_values() -> None:
    cache = NativeStageCache(10)
    first = StageCacheKey(stage="prompt", identity="a" * 64)
    second = StageCacheKey(stage="prompt", identity="b" * 64)
    cache.put(first, object(), 6)
    assert cache.used_bytes == 6
    cache.put(second, object(), 6)
    assert cache.get(first) is None
    assert cache.get(second) is not None
    assert cache.used_bytes == 6
    with pytest.raises(ValueError):
        cache.put(first, object(), 11)
    assert cache.used_bytes == 6


def test_encode_prompt_stores_joined_identity_once_without_prompt_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    def record(*args: object, **kwargs: object) -> None:
        assert not kwargs
        events.append(args)

    monkeypatch.setattr("coire_node.image_runtime.cache.record_image_cache", record)
    loaded, _model, request = _pipeline(max_bytes=4096)
    resolved = _resolved(request)
    loaded.encode_prompt(resolved)
    loaded.encode_prompt(resolved)
    assert [event[1] for event in events] == ["miss", "store", "hit"]
    assert all(event[0] == "prompt" for event in events)
    assert resolved.spec.prompt not in repr(events)
    parts = _parts(request, resolved, resolved.seeds[0])
    assert loaded.prompt_cache.get(_key(request, resolved, resolved.seeds[0])) == "\0".join(
        parts
    ).encode("utf-8")


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": "another prompt"},
        {"width": 128},
        {"height": 128},
        {"steps": 3},
        {"seed": 9},
    ],
)
def test_changed_prompt_stage_identity_misses(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    loaded, _model, request = _pipeline(max_bytes=4096)
    original = _resolved(request)
    loaded.encode_prompt(original)
    stored: list[StageCacheKey] = []
    original_put = loaded.prompt_cache.put

    def spy(key: StageCacheKey, payload: bytes) -> None:
        stored.append(key)
        original_put(key, payload)

    monkeypatch.setattr(loaded.prompt_cache, "put", spy)
    changed = _resolved(request, **changes)
    loaded.encode_prompt(changed)
    assert stored == [_key(request, changed, changed.seeds[0])]
    assert loaded.prompt_cache.get(_key(request, original, original.seeds[0])) is not None


def test_generate_encodes_before_denoising_and_still_calls_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, model, request = _pipeline(max_bytes=4096)
    resolved = _resolved(request, n=2)
    monkeypatch.setattr(pipeline, "_sync_latents", lambda value: None)
    order: list[str] = []
    original_encode = loaded.encode_prompt

    def track_encode(spec: ResolvedImageSpec) -> None:
        order.append("encode")
        original_encode(spec)

    monkeypatch.setattr(loaded, "encode_prompt", track_encode)
    original_generate = model.generate_image

    def track_generate(**kwargs: object) -> Image.Image:
        order.append("generate")
        return original_generate(**kwargs)

    monkeypatch.setattr(model, "generate_image", track_generate)
    images = loaded.generate(resolved, lambda index, step, total: None)
    assert order == ["encode", "generate", "generate"]
    assert [call["seed"] for call in model.calls] == [7, 8]
    assert all(image.mode == "RGB" and image.size == (64, 64) for image in images)
    for image in images:
        image.close()
    for seed in resolved.seeds:
        parts = _parts(request, resolved, seed)
        assert loaded.prompt_cache.get(_key(request, resolved, seed)) == "\0".join(parts).encode(
            "utf-8"
        )


def test_warm_prompt_cache_does_not_store_again_during_generate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, model, request = _pipeline(max_bytes=4096)
    resolved = _resolved(request)
    monkeypatch.setattr(pipeline, "_sync_latents", lambda value: None)
    loaded.encode_prompt(resolved)
    stored: list[bytes] = []
    original_put = loaded.prompt_cache.put

    def spy(key: StageCacheKey, payload: bytes) -> None:
        stored.append(payload)
        original_put(key, payload)

    monkeypatch.setattr(loaded.prompt_cache, "put", spy)
    images = loaded.generate(resolved, lambda index, step, total: None)
    assert stored == []
    assert len(model.calls) == 1
    for image in images:
        image.close()


def test_refused_generation_does_not_fill_the_prompt_cache() -> None:
    loaded, model, request = _pipeline(max_bytes=4096)
    with pytest.raises(pipeline.ImagePipelineUnavailable):
        loaded.generate(_resolved(request, guidance=Decimal(1)), lambda index, step, total: None)
    assert model.calls == []
    assert loaded.prompt_cache.occupancy("prompt") == 0


def test_replace_lora_keeps_one_adapter_and_drops_the_prompt_cache() -> None:
    loaded, _model, request = _pipeline(max_bytes=4096)
    resolved = _resolved(request)
    key = _key(request, resolved, resolved.seeds[0])
    loaded.encode_prompt(resolved)
    adapter_ids: list[str | None] = []
    cache_ids: list[int] = [id(loaded.prompt_cache)]
    present: list[bool] = []

    def replace(adapter_id: str | None) -> None:
        loaded.replace_lora(adapter_id)
        adapter_ids.append(loaded.adapter_id)
        cache_ids.append(id(loaded.prompt_cache))
        present.append(loaded.prompt_cache.get(key) is not None)

    replace("adapter-a")
    loaded.encode_prompt(resolved)
    replace("adapter-a")
    loaded.encode_prompt(resolved)
    replace("adapter-b")
    loaded.encode_prompt(resolved)
    replace(None)
    assert adapter_ids == ["adapter-a", "adapter-a", "adapter-b", None]
    assert present == [False, False, False, False]
    assert len(set(cache_ids)) == len(cache_ids)
    assert loaded.prompt_cache.max_bytes == 4096

    loaded.encode_prompt(resolved)
    kept = id(loaded.prompt_cache)
    with pytest.raises(ValueError, match="empty"):
        loaded.replace_lora("")
    assert loaded.adapter_id is None
    assert id(loaded.prompt_cache) == kept
    assert loaded.prompt_cache.get(key) is not None
