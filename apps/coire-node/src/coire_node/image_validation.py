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
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.models.registry import ModelKind
from coire_node.image_runtime.classification import (
    CLASSIFIER_REVISION,
    classify_image_with_peak,
)
from coire_node.image_runtime.pipeline import MfluxTxt2ImgPipeline
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
    if request.kind is not ModelKind.IMAGE_MODEL:
        raise ImageValidationUnavailable("auxiliary execution validation is unavailable")
    baseline_rss = psutil.Process().memory_info().rss
    pipeline = MfluxTxt2ImgPipeline.load(store, load)
    images = pipeline.generate(
        _smoke_spec(request.model_id, request.manifest_sha256), lambda *_: None
    )
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
    measured_rss = max(0, psutil.Process().memory_info().rss - baseline_rss)
    if measured_rss > reservation_bytes:
        raise ImageValidationUnavailable("image smoke exceeded reserved memory")
    profile = ImageCapabilityProfile(
        modes=(ImageMode.TXT2IMG,),
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
        peak_rss_bytes=measured_rss,
        thumbnail_sha256=thumbnail_sha256,
        image_capability_profile=profile,
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
