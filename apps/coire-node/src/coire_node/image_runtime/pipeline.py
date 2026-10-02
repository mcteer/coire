"""Fixed, local-only Z-Image Turbo execution inside a supervised Studio worker."""

from __future__ import annotations

import gc
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


def _load_native(
    path: Path,
    *,
    lora_paths: tuple[Path, ...] = (),
    lora_scales: tuple[float, ...] = (),
) -> _NativeModel:
    """Import mflux only after the node sets its credential-free offline environment."""
    _require_offline()
    model_module = importlib.import_module("mflux.models.z_image.variants.z_image")
    config_module = importlib.import_module("mflux.models.common.config.model_config")
    config = config_module.ModelConfig.z_image_turbo()
    if config.supports_guidance:
        raise ImagePipelineUnavailable()
    if len(lora_paths) != len(lora_scales) or any(not item.is_file() for item in lora_paths):
        raise ImagePipelineUnavailable()
    model = model_module.ZImage(
        model_path=str(path),
        model_config=config,
        lora_paths=[str(item) for item in lora_paths] or None,
        lora_scales=list(lora_scales) or None,
    )
    # Materialize lazy weights on their creating thread before execution moves
    # to the worker thread. MLX streams cannot be evaluated across threads.
    mlx = importlib.import_module("mlx.core")
    mlx.eval(model.parameters())
    return cast("_NativeModel", model)


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
        store: Store | None = None,
    ) -> None:
        self._model = model
        self._request = request
        self._store = store
        self._callback = _ProgressCallback()
        self._lock = threading.Lock()
        self.prompt_cache = StageCache(prompt_cache_max_bytes)
        self.encoder_cache = NativeStageCache(prompt_cache_max_bytes)
        self._adapter_id: str | None = None
        self._active_prompt_identity: str | None = None
        self._native_encoder_hook = False
        self._original_encoder: Callable[..., tuple[object, object | None]] | None = None
        self._attach_model(model)

    def _attach_model(self, model: _NativeModel) -> None:
        self._model = model
        model.callbacks.register(self._callback)
        self._native_encoder_hook = False
        self._original_encoder = None
        native_encoder = getattr(model, "_encode_prompts", None)
        if callable(native_encoder):
            encoder = cast(Callable[..., tuple[object, object | None]], native_encoder)
            self._original_encoder = encoder

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
    def load(
        cls,
        store: Store,
        request: ImageWorkerLoadRequest,
        *,
        prompt_cache_max_bytes: int = _DEFAULT_PROMPT_CACHE_BYTES,
    ) -> MfluxTxt2ImgPipeline:
        _require_offline()
        path = verify_image_copy(store, request)
        return cls(
            _load_native(path),
            request,
            prompt_cache_max_bytes=prompt_cache_max_bytes,
            store=store,
        )

    @property
    def adapter_id(self) -> str | None:
        return self._adapter_id

    def _replace_lora_locked(self, resolved: ResolvedImageSpec) -> None:
        """Rebuild from the verified clean base whenever the ordered stack changes."""
        if (
            resolved.spec.model_id != self._request.model_id
            or resolved.spec.variant_id != self._request.variant_id
            or resolved.model_sha256 != self._request.manifest_sha256
            or resolved.pipeline_version != self._request.runtime_version
        ):
            raise ImagePipelineUnavailable()
        if self._store is None:
            if resolved.spec.loras or resolved.dependencies:
                raise ImagePipelineUnavailable()
            return
        spec = resolved.spec
        if len(spec.loras) != len(resolved.dependencies):
            raise ImagePipelineUnavailable()
        if {item.model_id for item in spec.loras} != {
            item.model_id for item in resolved.dependencies
        }:
            raise ImagePipelineUnavailable()
        dependencies = {item.model_id: item for item in resolved.dependencies}
        paths: list[Path] = []
        scales: list[float] = []
        identity: list[str] = [self._request.manifest_sha256]
        for adapter in spec.loras:
            dependency = dependencies[adapter.model_id]
            if (
                adapter.variant_id is not None
                or dependency.variant_id is not None
                or dependency.slug is None
            ):
                raise ImagePipelineUnavailable()
            load = ImageWorkerLoadRequest(
                slug=dependency.slug,
                model_id=dependency.model_id,
                instance_id=self._request.instance_id,
                manifest_sha256=dependency.sha256,
                reservation_bytes=self._request.reservation_bytes,
                runtime_version=self._request.runtime_version,
            )
            path = verify_image_copy(self._store, load)
            manifest = self._store.read_manifest(dependency.slug)
            if manifest is None or manifest.revision != dependency.revision:
                raise ImagePipelineUnavailable()
            weights = [
                entry.path for entry in manifest.files if entry.path.endswith(".safetensors")
            ]
            if len(weights) != 1:
                raise ImagePipelineUnavailable()
            paths.append(path / weights[0])
            scales.append(float(adapter.scale))
            identity.extend((str(adapter.model_id), dependency.sha256, str(adapter.scale)))
        wanted = stage_identity(*identity) if spec.loras else None
        if wanted == self._adapter_id:
            return
        base_path = verify_image_copy(self._store, self._request)
        previous = self._model
        if self._original_encoder is not None:
            previous._encode_prompts = self._original_encoder
        self._original_encoder = None
        self._model = cast("_NativeModel", None)
        self._callback.end()
        max_bytes = self.prompt_cache.max_bytes
        self.prompt_cache = StageCache(max_bytes)
        self.encoder_cache = NativeStageCache(max_bytes)
        self._adapter_id = None
        del previous
        gc.collect()
        mlx = importlib.import_module("mlx.core")
        mlx.clear_cache()
        self._attach_model(
            _load_native(base_path, lora_paths=tuple(paths), lora_scales=tuple(scales))
        )
        self._adapter_id = wanted

    def replace_lora(self, resolved: ResolvedImageSpec) -> None:
        if not self._lock.acquire(blocking=False):
            raise ImagePipelineUnavailable()
        try:
            self._replace_lora_locked(resolved)
        finally:
            self._lock.release()

    def encode_prompt(self, resolved: ResolvedImageSpec) -> None:
        """Remember the prompt stage for each seed. Callers still run the denoiser."""
        if self.prompt_cache.max_bytes == 0:
            return
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
            self._replace_lora_locked(resolved)
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
