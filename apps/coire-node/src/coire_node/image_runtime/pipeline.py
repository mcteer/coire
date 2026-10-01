"""Fixed, local-only Z-Image Turbo execution inside a supervised Studio worker."""

from __future__ import annotations

import importlib
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import ImageMode, ResolvedImageSpec
from coire_node.image_runtime.cache import (
    NativeStageCache,
    StageCache,
    StageCacheKey,
    stage_identity,
)
from coire_node.image_runtime.preflight import RUNTIME_VERSION, verify_image_copy
from coire_node.store import Store

Progress = Callable[[int, int, int], None]
_DEFAULT_PROMPT_CACHE_BYTES = 256 * 1024**2


class ImagePipelineUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("image pipeline unavailable")


class _CallbackRegistry(Protocol):
    def register(self, callback: object) -> None: ...


class _NativeModel(Protocol):
    callbacks: _CallbackRegistry
    _encode_prompts: Callable[..., tuple[object, object | None]]

    def generate_image(
        self,
        *,
        seed: int,
        prompt: str,
        num_inference_steps: int,
        height: int,
        width: int,
        guidance: float,
        negative_prompt: None,
        image_path: Path | None = None,
        image_strength: float | None = None,
    ) -> object: ...


def _require_offline() -> None:
    if (
        os.environ.get("HF_HUB_OFFLINE") != "1"
        or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    ):
        raise ImagePipelineUnavailable()


def _load_native(path: Path) -> _NativeModel:
    """Import mflux only after the node sets its credential-free offline environment."""
    _require_offline()
    model_module = importlib.import_module("mflux.models.z_image.variants.z_image")
    config_module = importlib.import_module("mflux.models.common.config.model_config")
    config = config_module.ModelConfig.z_image_turbo()
    if config.supports_guidance:
        raise ImagePipelineUnavailable()
    return cast("_NativeModel", model_module.ZImage(model_path=str(path), model_config=config))


def _sync_latents(latents: object) -> None:
    mlx = importlib.import_module("mlx.core")
    mlx.eval(latents)


def _sync_encodings(positive: object, negative: object | None) -> None:
    mlx = importlib.import_module("mlx.core")
    if negative is None:
        mlx.eval(positive)
    else:
        mlx.eval(positive, negative)


class _ProgressCallback:
    def __init__(self) -> None:
        self.report: Progress | None = None
        self.output_index = 0
        self.total = 0
        self.completed = 0
        self.seed = 0
        self.offset = 0
        self.report_total = 0

    def begin(
        self,
        *,
        index: int,
        total: int,
        seed: int,
        report: Progress,
        offset: int = 0,
        report_total: int | None = None,
    ) -> None:
        self.output_index = index
        self.total = total
        self.completed = 0
        self.seed = seed
        self.report = report
        self.offset = offset
        self.report_total = report_total if report_total is not None else total

    def call_in_loop(
        self,
        *,
        t: int,
        seed: int,
        prompt: str,
        latents: object,
        config: object,
        time_steps: object,
    ) -> None:
        del t, prompt, config, time_steps
        if self.report is None or seed != self.seed or self.completed >= self.total:
            raise ImagePipelineUnavailable()
        _sync_latents(latents)
        self.completed += 1
        self.report(self.output_index, self.offset + self.completed, self.report_total)

    def end(self) -> None:
        self.report = None


class MfluxTxt2ImgPipeline:
    """One resident, serialized base model; process lifetime belongs to coire-node."""

    def __init__(
        self,
        model: _NativeModel,
        request: ImageWorkerLoadRequest,
        *,
        prompt_cache_max_bytes: int = _DEFAULT_PROMPT_CACHE_BYTES,
    ) -> None:
        self._model = model
        self._request = request
        self._callback = _ProgressCallback()
        self._model.callbacks.register(self._callback)
        self._lock = threading.Lock()
        self.prompt_cache = StageCache(prompt_cache_max_bytes)
        self.encoder_cache = NativeStageCache(prompt_cache_max_bytes)
        self._adapter_id: str | None = None
        self._active_prompt_identity: str | None = None
        self._native_encoder_hook = False
        native_encoder = getattr(model, "_encode_prompts", None)
        if callable(native_encoder):
            encoder = cast(Callable[..., tuple[object, object | None]], native_encoder)

            def cached_encoder(
                *, prompt: str, negative_prompt: str | None, guidance: float
            ) -> tuple[object, object | None]:
                active = self._active_prompt_identity
                if active is None:
                    return encoder(
                        prompt=prompt, negative_prompt=negative_prompt, guidance=guidance
                    )
                key = StageCacheKey(
                    stage="prompt",
                    identity=stage_identity(active, prompt, negative_prompt or "", repr(guidance)),
                )
                hit = self.encoder_cache.get(key)
                if hit is not None:
                    return cast(tuple[object, object | None], hit)
                result = encoder(prompt=prompt, negative_prompt=negative_prompt, guidance=guidance)
                positive, negative = result
                _sync_encodings(positive, negative)
                size = int(getattr(positive, "nbytes", 0)) + (
                    int(getattr(negative, "nbytes", 0)) if negative is not None else 0
                )
                if 0 < size <= self.encoder_cache.max_bytes:
                    self.encoder_cache.put(key, result, size)
                return result

            model._encode_prompts = cached_encoder
            self._native_encoder_hook = True

    @classmethod
    def load(cls, store: Store, request: ImageWorkerLoadRequest) -> MfluxTxt2ImgPipeline:
        _require_offline()
        path = verify_image_copy(store, request)
        return cls(_load_native(path), request)

    @property
    def adapter_id(self) -> str | None:
        return self._adapter_id

    def replace_lora(self, adapter_id: str | None) -> None:
        """Keep one adapter id. Replacement and unload drop the prompt cache."""
        if adapter_id == "":
            raise ValueError("lora adapter id must not be empty")
        with self._lock:
            max_bytes = self.prompt_cache.max_bytes
            self._adapter_id = adapter_id
            self.prompt_cache = StageCache(max_bytes)
            self.encoder_cache = NativeStageCache(max_bytes)

    def encode_prompt(self, resolved: ResolvedImageSpec) -> None:
        """Remember the prompt stage for each seed. Callers still run the denoiser."""
        spec = resolved.spec
        for seed in resolved.seeds:
            parts = (
                self._request.runtime_version,
                self._request.manifest_sha256,
                spec.prompt,
                str(spec.width),
                str(spec.height),
                str(spec.steps),
                str(seed),
            )
            key = StageCacheKey(stage="prompt", identity=stage_identity(*parts))
            if self.prompt_cache.get(key) is None:
                self.prompt_cache.put(key, "\0".join(parts).encode("utf-8"))

    def generate(
        self,
        resolved: ResolvedImageSpec,
        on_progress: Progress,
        *,
        input_paths: dict[uuid.UUID, Path] | None = None,
    ) -> tuple[Image.Image, ...]:
        spec = resolved.spec
        init_path: Path | None = None
        if spec.mode is ImageMode.IMG2IMG:
            if (
                spec.init_image_id is None
                or spec.strength is None
                or input_paths is None
                or set(input_paths) != {spec.init_image_id}
                or {item.input_id for item in resolved.inputs} != {spec.init_image_id}
            ):
                raise ImagePipelineUnavailable()
            init_path = input_paths[spec.init_image_id]
        elif input_paths:
            raise ImagePipelineUnavailable()
        if (
            resolved.pipeline_version != RUNTIME_VERSION
            or resolved.model_sha256 != self._request.manifest_sha256
            or spec.model_id != self._request.model_id
            or spec.variant_id != self._request.variant_id
            or spec.mode not in {ImageMode.TXT2IMG, ImageMode.IMG2IMG}
            or spec.guidance != 0
            or spec.negative_prompt is not None
            or spec.loras
            or spec.mask_id is not None
            or spec.control is not None
            or spec.upscale is not None
            or (spec.mode is ImageMode.TXT2IMG and resolved.inputs)
        ):
            raise ImagePipelineUnavailable()
        if not self._lock.acquire(blocking=False):
            raise ImagePipelineUnavailable()
        images: list[Image.Image] = []
        try:
            if self._native_encoder_hook:
                self._active_prompt_identity = stage_identity(
                    self._request.runtime_version,
                    self._request.manifest_sha256,
                    str(self._request.variant_id),
                    resolved.environment_fingerprint,
                    self._adapter_id or "",
                    *(dependency.sha256 for dependency in resolved.dependencies),
                )
            else:
                self.encode_prompt(resolved)
            offset = (
                max(1, int(spec.steps * float(spec.strength)))
                if init_path is not None and spec.strength is not None
                else 0
            )
            denoise_steps = spec.steps - offset
            for index, seed in enumerate(resolved.seeds):
                self._callback.begin(
                    index=index,
                    total=denoise_steps,
                    seed=seed,
                    report=on_progress,
                    offset=offset,
                    report_total=spec.steps,
                )
                try:
                    generated = self._model.generate_image(
                        seed=seed,
                        prompt=spec.prompt,
                        num_inference_steps=spec.steps,
                        height=spec.height,
                        width=spec.width,
                        guidance=0.0,
                        negative_prompt=None,
                        image_path=init_path,
                        image_strength=float(spec.strength) if spec.strength is not None else None,
                    )
                    if self._callback.completed != denoise_steps:
                        raise ImagePipelineUnavailable()
                    if denoise_steps == 0:
                        on_progress(index, spec.steps, spec.steps)
                    image = (
                        generated
                        if isinstance(generated, Image.Image)
                        else getattr(generated, "image", None)
                    )
                    if not isinstance(image, Image.Image):
                        raise ImagePipelineUnavailable()
                    if image.mode != "RGB" or image.size != (spec.width, spec.height):
                        image.close()
                        raise ImagePipelineUnavailable()
                    images.append(image)
                finally:
                    self._callback.end()
            return tuple(images)
        except Exception:
            for image in images:
                image.close()
            raise
        finally:
            self._active_prompt_identity = None
            self._lock.release()
