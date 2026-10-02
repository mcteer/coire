"""Offline, reserved Studio validation before an image base may be published."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil
from opentelemetry import metrics, trace
from PIL import Image, ImageStat

from coire_core.models.image_worker import (
    ImageAssetValidateRequest,
    ImageAssetValidationResult,
    ImageWorkerLoadRequest,
)
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentTag,
    ImageInputDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.models.registry import ModelKind
from coire_node.footprint import resident_bytes
from coire_node.image_runtime.classification import (
    CLASSIFIER_REVISION,
    classify_image_with_peak,
)
from coire_node.image_runtime.pipeline import MfluxTxt2ImgPipeline, _load_native
from coire_node.image_runtime.preflight import RUNTIME_VERSION, verify_image_copy
from coire_node.store import Store

_SMOKE_SIZE = 512
_SMOKE_STEPS = 4
_SMOKE_PROMPT = "a blue square on a plain background"
_tracer = trace.get_tracer("coire.node.image_validation")
_validation_total = metrics.get_meter("coire.node.image_validation").create_counter(
    "coire_image_validation_total",
    unit="1",
    description="Reserved Studio image validation outcomes",
)


class ImageValidationUnavailable(RuntimeError):
    pass


def _smoke_spec(model_id: uuid.UUID, manifest_sha256: str) -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=model_id,
        mode=ImageMode.TXT2IMG,
        prompt=_SMOKE_PROMPT,
        width=_SMOKE_SIZE,
        height=_SMOKE_SIZE,
        steps=_SMOKE_STEPS,
        guidance=Decimal(0),
        seed=1,
    )
    return ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version=RUNTIME_VERSION,
        environment_fingerprint="0" * 64,
        model_sha256=manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )


def _thumbnail_digest(image: Image.Image) -> str:
    thumb = image.copy()
    try:
        thumb.thumbnail((64, 64))
        with io.BytesIO() as buffer:
            thumb.save(buffer, format="PNG")
            return hashlib.sha256(buffer.getvalue()).hexdigest()
    finally:
        thumb.close()


def validate_image_asset(
    store: Store, request: ImageAssetValidateRequest, *, reservation_bytes: int
) -> ImageAssetValidationResult:
    """Verify exact local bytes, then perform one neutral native base-model smoke."""
    with _tracer.start_as_current_span("coire.node.image_validate") as span:
        span.set_attribute("coire.model_id", str(request.model_id))
        span.set_attribute("coire.job_id", str(request.job_id))
        span.set_attribute("coire.kind", request.kind.value)
        try:
            result = _validate_image_asset(store, request, reservation_bytes=reservation_bytes)
        except Exception:
            _validation_total.add(1, {"kind": request.kind.value, "outcome": "failed"})
            raise
        _validation_total.add(1, {"kind": request.kind.value, "outcome": "validated"})
        return result


def _validate_image_asset(
    store: Store, request: ImageAssetValidateRequest, *, reservation_bytes: int
) -> ImageAssetValidationResult:
    if reservation_bytes <= 0:
        raise ImageValidationUnavailable("validation reservation is unavailable")
    manifest = store.read_manifest(request.slug)
    if (
        manifest is None
        or manifest.revision != request.source_revision
        or manifest.repo_id == ""
        or manifest.sha256() != request.manifest_sha256
    ):
        raise ImageValidationUnavailable("image manifest differs from pinned source")
    load = ImageWorkerLoadRequest(
        slug=request.slug,
        model_id=request.model_id,
        instance_id=request.job_id,
        manifest_sha256=request.manifest_sha256,
        reservation_bytes=reservation_bytes,
        runtime_version=RUNTIME_VERSION,
    )
    path = verify_image_copy(store, load)
    if request.kind is ModelKind.IMAGE_CLASSIFIER:
        return _validate_classifier(path, request, reservation_bytes=reservation_bytes)
    if request.kind is ModelKind.IMAGE_LORA:
        return _validate_lora(store, path, request, reservation_bytes=reservation_bytes)
    if request.kind is not ModelKind.IMAGE_MODEL:
        raise ImageValidationUnavailable("auxiliary execution validation is unavailable")
    process = psutil.Process()
    baseline_rss = process.memory_info().rss
    baseline_physical = resident_bytes(process.pid)
    if baseline_physical is None:
        raise ImageValidationUnavailable("physical footprint is unavailable")
    peak_rss = baseline_rss
    peak_physical = baseline_physical

    def observe_footprint(*_: int) -> None:
        nonlocal peak_rss, peak_physical
        current = resident_bytes(process.pid)
        if current is None:
            raise ImageValidationUnavailable("physical footprint is unavailable")
        peak_rss = max(peak_rss, process.memory_info().rss)
        peak_physical = max(peak_physical, current)

    pipeline = MfluxTxt2ImgPipeline.load(store, load)
    observe_footprint()
    images = pipeline.generate(
        _smoke_spec(request.model_id, request.manifest_sha256), observe_footprint
    )
    observe_footprint()
    try:
        if (
            len(images) != 1
            or images[0].getbbox() is None
            or max(ImageStat.Stat(images[0]).stddev) <= 1
        ):
            raise ImageValidationUnavailable("image smoke returned degenerate pixels")
        thumbnail_sha256 = _thumbnail_digest(images[0])
    finally:
        for image in images:
            image.close()
    with TemporaryDirectory(prefix="coire-image-smoke-") as directory:
        input_id = uuid.uuid4()
        source = Path(directory) / f"{input_id}.png"
        with Image.new("RGB", (_SMOKE_SIZE, _SMOKE_SIZE), (32, 96, 192)) as sample:
            sample.save(source, format="PNG")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        base = _smoke_spec(request.model_id, request.manifest_sha256)
        spec = base.spec.model_copy(
            update={
                "mode": ImageMode.IMG2IMG,
                "init_image_id": input_id,
                "strength": Decimal("0.375125"),
            }
        )
        resolved = base.model_copy(
            update={
                "spec": spec,
                "spec_hash": canonical_spec_hash(spec),
                "inputs": (
                    ImageInputDigest(
                        input_id=input_id,
                        sha256=digest,
                        width=_SMOKE_SIZE,
                        height=_SMOKE_SIZE,
                    ),
                ),
            }
        )
        transformed = pipeline.generate(resolved, observe_footprint, input_paths={input_id: source})
        observe_footprint()
        try:
            if (
                len(transformed) != 1
                or transformed[0].mode != "RGB"
                or transformed[0].size != (_SMOKE_SIZE, _SMOKE_SIZE)
                or transformed[0].getbbox() is None
                or max(ImageStat.Stat(transformed[0]).stddev) <= 1
            ):
                raise ImageValidationUnavailable("img2img smoke returned degenerate pixels")
        finally:
            for image in transformed:
                image.close()
    measured_physical = max(0, peak_physical - baseline_physical)
    if max(peak_rss, peak_physical) > reservation_bytes:
        raise ImageValidationUnavailable("image smoke exceeded reserved memory")
    profile = ImageCapabilityProfile(
        modes=(ImageMode.TXT2IMG, ImageMode.IMG2IMG),
        min_width=_SMOKE_SIZE,
        max_width=_SMOKE_SIZE,
        min_height=_SMOKE_SIZE,
        max_height=_SMOKE_SIZE,
        max_pixels=_SMOKE_SIZE * _SMOKE_SIZE,
        min_steps=_SMOKE_STEPS,
        max_steps=_SMOKE_STEPS,
        min_guidance=Decimal(0),
        max_guidance=Decimal(0),
        max_outputs=1,
        default_width=_SMOKE_SIZE,
        default_height=_SMOKE_SIZE,
        default_steps=_SMOKE_STEPS,
        default_guidance=Decimal(0),
    )
    return ImageAssetValidationResult(
        validated=True,
        kind=request.kind,
        manifest_sha256=request.manifest_sha256,
        source_revision=request.source_revision,
        peak_rss_bytes=peak_rss,
        peak_physical_bytes=peak_physical,
        peak_physical_delta_bytes=measured_physical,
        thumbnail_sha256=thumbnail_sha256,
        image_capability_profile=profile,
    )


def _validate_lora(
    store: Store,
    path: Path,
    request: ImageAssetValidateRequest,
    *,
    reservation_bytes: int,
) -> ImageAssetValidationResult:
    """Apply one exact local adapter to a clean verified base and run a native smoke."""
    base = request.compatible_base
    if base is None:
        raise ImageValidationUnavailable("compatible base is unavailable")
    base_manifest = store.read_manifest(base.slug)
    adapter_manifest = store.read_manifest(request.slug)
    if (
        base_manifest is None
        or base_manifest.revision != base.source_revision
        or adapter_manifest is None
        or adapter_manifest.revision != request.source_revision
    ):
        raise ImageValidationUnavailable("compatible image source differs from pinned copy")
    base_load = ImageWorkerLoadRequest(
        slug=base.slug,
        model_id=base.model_id,
        instance_id=request.job_id,
        manifest_sha256=base.manifest_sha256,
        reservation_bytes=reservation_bytes,
        runtime_version=RUNTIME_VERSION,
    )
    base_path = verify_image_copy(store, base_load)
    adapters = [
        entry.path for entry in adapter_manifest.files if entry.path.endswith(".safetensors")
    ]
    if len(adapters) != 1:
        raise ImageValidationUnavailable("LoRA asset must contain one safetensors adapter")
    process = psutil.Process()
    baseline_physical = resident_bytes(process.pid)
    if baseline_physical is None:
        raise ImageValidationUnavailable("physical footprint is unavailable")
    peak_rss = process.memory_info().rss
    peak_physical = baseline_physical

    def observe_footprint(*_: int) -> None:
        nonlocal peak_rss, peak_physical
        current = resident_bytes(process.pid)
        if current is None:
            raise ImageValidationUnavailable("physical footprint is unavailable")
        peak_rss = max(peak_rss, process.memory_info().rss)
        peak_physical = max(peak_physical, current)

    adapter_path = path / adapters[0]
    pipeline = MfluxTxt2ImgPipeline(
        _load_native(base_path, lora_paths=(adapter_path,), lora_scales=(1.0,)), base_load
    )
    observe_footprint()
    images = pipeline.generate(_smoke_spec(base.model_id, base.manifest_sha256), observe_footprint)
    observe_footprint()
    try:
        if (
            len(images) != 1
            or images[0].mode != "RGB"
            or images[0].size != (_SMOKE_SIZE, _SMOKE_SIZE)
            or images[0].getbbox() is None
            or max(ImageStat.Stat(images[0]).stddev) <= 1
        ):
            raise ImageValidationUnavailable("LoRA smoke returned degenerate pixels")
        thumbnail_sha256 = _thumbnail_digest(images[0])
    finally:
        for image in images:
            image.close()
    if max(peak_rss, peak_physical) > reservation_bytes:
        raise ImageValidationUnavailable("LoRA smoke exceeded reserved memory")
    return ImageAssetValidationResult(
        validated=True,
        kind=request.kind,
        manifest_sha256=request.manifest_sha256,
        source_revision=request.source_revision,
        peak_rss_bytes=peak_rss,
        peak_physical_bytes=peak_physical,
        peak_physical_delta_bytes=max(0, peak_physical - baseline_physical),
        thumbnail_sha256=thumbnail_sha256,
    )


def _validate_classifier(
    path: Path, request: ImageAssetValidateRequest, *, reservation_bytes: int
) -> ImageAssetValidationResult:
    """Exercise the pinned classifier on a synthetic local image before publication."""
    if request.source_revision != CLASSIFIER_REVISION:
        raise ImageValidationUnavailable("classifier revision differs from pinned policy")
    with TemporaryDirectory(prefix="coire-classifier-smoke-") as directory:
        sample = Path(directory) / "smoke.png"
        with Image.new("RGB", (64, 64), (32, 96, 192)) as image:
            image.save(sample, format="PNG")
        result, measured_rss = asyncio.run(
            classify_image_with_peak(
                path,
                sample,
                reservation_bytes=reservation_bytes,
                job_id=str(request.job_id),
            )
        )
    if (
        result.tag not in {ImageContentTag.NORMAL, ImageContentTag.EXPLICIT}
        or result.score is None
        or result.safe_error is not None
        or result.classifier_revision != CLASSIFIER_REVISION
        or result.processor_sha256 is None
    ):
        raise ImageValidationUnavailable("classifier smoke returned invalid evidence")
    if measured_rss > reservation_bytes:
        raise ImageValidationUnavailable("classifier smoke exceeded reserved memory")
    return ImageAssetValidationResult(
        validated=True,
        kind=request.kind,
        manifest_sha256=request.manifest_sha256,
        source_revision=request.source_revision,
        peak_rss_bytes=measured_rss,
    )
