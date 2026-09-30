"""Bounded, content-free telemetry for private image API operations."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from opentelemetry import metrics, trace
from opentelemetry.trace import Span

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.image")
meter = metrics.get_meter("coire.api.image")
requests_total = meter.create_counter(
    "coire_image_requests_total", unit="1", description="Private image API requests"
)
jobs_total = meter.create_counter(
    "coire_image_jobs_total", unit="1", description="Private image terminal outcomes"
)
stage_seconds = meter.create_histogram(
    "coire_image_stage_seconds", unit="s", description="Private image stage duration"
)
purge_oldest_seconds = meter.create_gauge(
    "coire_image_purge_oldest_seconds",
    unit="s",
    description="Age of oldest image blob awaiting verified physical purge",
)

_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")


class ImageOperation(StrEnum):
    SUBMIT = "submit"
    INPUT_UPLOAD = "input_upload"
    TRANSFER = "transfer"
    DOWNLOAD = "download"
    CANCEL = "cancel"
    DELETE = "delete"
    PRESET_MUTATION = "preset_mutation"


class ImageOutcome(StrEnum):
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"


class ImageReason(StrEnum):
    NONE = "none"
    AUTH = "auth"
    QUOTA = "quota"
    SIZE = "size"
    FORMAT = "format"
    STORAGE = "storage"
    DEPENDENCY = "dependency"
    CONFLICT = "conflict"
    INTERNAL = "internal"


class ImageStage(StrEnum):
    QUEUE = "queue"
    LOAD = "load"
    PREPROCESS = "preprocess"
    GENERATE = "generate"
    UPSCALE = "upscale"
    CLASSIFY = "classify"
    TRANSFER = "transfer"
    PUBLISH = "publish"
    PURGE = "purge"


def _validated_job_id(job_id: str | None) -> str | None:
    if job_id is not None and _ULID.fullmatch(job_id) is None:
        raise ValueError("invalid image job identifier")
    return job_id


def record_image_request(
    operation: ImageOperation,
    outcome: ImageOutcome,
    *,
    reason: ImageReason = ImageReason.NONE,
    job_id: str | None = None,
) -> None:
    """Record a fixed operation/outcome/reason set; never accept content as a label."""
    if not isinstance(operation, ImageOperation):
        raise ValueError("unknown image operation")
    if not isinstance(outcome, ImageOutcome):
        raise ValueError("unknown image outcome")
    if not isinstance(reason, ImageReason):
        raise ValueError("unknown image reason")
    job_id = _validated_job_id(job_id)
    requests_total.add(
        1,
        attributes={
            "operation": operation.value,
            "outcome": outcome.value,
            "reason": reason.value,
        },
    )
    logger.info(
        "image request",
        extra={
            "image_operation": operation.value,
            "image_outcome": outcome.value,
            "image_reason": reason.value,
            "job_id": job_id,
        },
    )


def record_image_job(outcome: ImageOutcome) -> None:
    if not isinstance(outcome, ImageOutcome) or outcome not in {
        ImageOutcome.SUCCEEDED,
        ImageOutcome.FAILED,
        ImageOutcome.CANCELLED,
    }:
        raise ValueError("unknown image terminal outcome")
    jobs_total.add(1, attributes={"outcome": outcome.value})


def record_image_stage(stage: ImageStage, duration_s: float) -> None:
    if not isinstance(stage, ImageStage) or not math.isfinite(duration_s) or duration_s < 0:
        raise ValueError("invalid image stage measurement")
    stage_seconds.record(duration_s, attributes={"stage": stage.value})


def set_purge_oldest_seconds(age_s: float) -> None:
    if not math.isfinite(age_s) or age_s < 0:
        raise ValueError("invalid image purge age")
    purge_oldest_seconds.set(age_s)


@contextmanager
def image_span(operation: ImageOperation, *, job_id: str | None = None) -> Iterator[Span]:
    """Start a service operation span with a fixed name and optional validated job ID."""
    if not isinstance(operation, ImageOperation):
        raise ValueError("unknown image operation")
    job_id = _validated_job_id(job_id)
    with tracer.start_as_current_span(f"coire.api.image.{operation.value}") as span:
        if job_id is not None:
            span.set_attribute("job_id", job_id)
        yield span
