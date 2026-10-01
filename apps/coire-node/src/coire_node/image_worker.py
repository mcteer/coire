"""One fenced image job attempt inside a resident, supervised Studio worker."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest, ImageWorkerRunRequest
from coire_core.models.images import ResolvedImageSpec
from coire_node.image_runtime.metadata import EncodedImageOutput, write_image_png
from coire_node.metrics import (
    ImageNodeOutcome,
    ImageNodeStage,
    image_node_span,
    record_image_stage,
)

Progress = Callable[[int, int, int], None]


class ImageJobExecutionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("image job failed")


class ImageJobCancelled(RuntimeError):
    """Cooperative cancellation at a synchronized generation step."""


class _ImagePipeline(Protocol):
    def generate(
        self, resolved: ResolvedImageSpec, on_progress: Progress
    ) -> tuple[Image.Image, ...]: ...


class _InputImagePipeline(Protocol):
    def generate(
        self,
        resolved: ResolvedImageSpec,
        on_progress: Progress,
        *,
        input_paths: dict[uuid.UUID, Path],
    ) -> tuple[Image.Image, ...]: ...


@dataclass(frozen=True)
class GeneratedOutput:
    index: int
    path: Path
    encoded: EncodedImageOutput


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _private_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ImageJobExecutionError()


def _verified_input_paths(
    scratch_root: Path, request: ImageWorkerRunRequest
) -> dict[uuid.UUID, Path]:
    root = scratch_root.parent / "image-input-scratch"
    attempt = root / f"{request.job_id}-{request.attempt}-{request.fence}"
    for directory in (root, attempt):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ImageJobExecutionError()
    result: dict[uuid.UUID, Path] = {}
    for item in request.inputs:
        path = attempt / str(item.input_id)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size != item.byte_count
            ):
                raise ImageJobExecutionError()
            with os.fdopen(fd, "rb", closefd=False) as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != item.sha256:
                    raise ImageJobExecutionError()
        finally:
            os.close(fd)
        result[item.input_id] = path
    return result


def _run_attempt(
    pipeline: _ImagePipeline,
    load: ImageWorkerLoadRequest,
    request: ImageWorkerRunRequest,
    scratch_root: Path,
    on_progress: Progress,
    *,
    monotonic: Callable[[], float],
    utc_now: Callable[[], datetime],
) -> tuple[GeneratedOutput, ...]:
    resolved = request.resolved
    if (
        request.instance_id != load.instance_id
        or resolved.spec.model_id != load.model_id
        or resolved.spec.variant_id != load.variant_id
        or resolved.model_sha256 != load.manifest_sha256
        or resolved.pipeline_version != load.runtime_version
        or utc_now() >= request.deadline_at
    ):
        raise ImageJobExecutionError()
    _private_root(scratch_root)
    attempt_dir = scratch_root / f"{request.job_id}-{request.attempt}-{request.fence}"
    attempt_dir.mkdir(mode=0o700)
    images: tuple[Image.Image, ...] = ()
    last_reported: float | None = None

    def progress(index: int, step: int, total: int) -> None:
        nonlocal last_reported
        if utc_now() >= request.deadline_at or not 0 <= index < len(resolved.seeds):
            raise ImageJobExecutionError()
        if total != resolved.spec.steps or not 1 <= step <= total:
            raise ImageJobExecutionError()
        now = monotonic()
        if step == total or last_reported is None or now - last_reported >= 0.25:
            on_progress(index, step, total)
            last_reported = now

    try:
        if request.inputs:
            paths = _verified_input_paths(scratch_root, request)
            images = cast(_InputImagePipeline, pipeline).generate(
                resolved, progress, input_paths=paths
            )
        else:
            images = pipeline.generate(resolved, progress)
        if len(images) != len(resolved.seeds) or utc_now() >= request.deadline_at:
            raise ImageJobExecutionError()
        results: list[GeneratedOutput] = []
        for index, image in enumerate(images):
            if utc_now() >= request.deadline_at:
                raise ImageJobExecutionError()
            path = attempt_dir / f"{index}.png"
            encoded = write_image_png(image, resolved, index, path)
            results.append(GeneratedOutput(index=index, path=path, encoded=encoded))
        return tuple(results)
    except Exception:
        shutil.rmtree(attempt_dir)
        raise
    finally:
        for image in images:
            image.close()


def run_image_job(
    pipeline: _ImagePipeline,
    load: ImageWorkerLoadRequest,
    request: ImageWorkerRunRequest,
    scratch_root: Path,
    on_progress: Progress,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = _utc_now,
) -> tuple[GeneratedOutput, ...]:
    """Execute once; replay cannot overwrite a completed or partial attempt directory."""
    started = time.monotonic()
    with image_node_span(ImageNodeStage.GENERATE, job_id=request.job_id):
        try:
            result = _run_attempt(
                pipeline,
                load,
                request,
                scratch_root,
                on_progress,
                monotonic=monotonic,
                utc_now=utc_now,
            )
        except ImageJobCancelled:
            record_image_stage(
                ImageNodeStage.GENERATE,
                ImageNodeOutcome.CANCELLED,
                duration_s=time.monotonic() - started,
                job_id=request.job_id,
            )
            raise ImageJobExecutionError() from None
        except Exception:
            record_image_stage(
                ImageNodeStage.GENERATE,
                ImageNodeOutcome.FAILED,
                duration_s=time.monotonic() - started,
                job_id=request.job_id,
            )
            raise ImageJobExecutionError() from None
        record_image_stage(
            ImageNodeStage.GENERATE,
            ImageNodeOutcome.SUCCEEDED,
            duration_s=time.monotonic() - started,
            job_id=request.job_id,
        )
        return result
