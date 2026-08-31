"""Node routes for acquisition jobs.

Every start verb is idempotent on the control plane's `job_id`: a restarted control plane
re-issues the current stage, and must get the existing job rather than a second download
(ADR-0005).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from opentelemetry import metrics, trace
from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.acquisition import Reservation, ReservationRequest
from coire_core.models.jobs import ChecksumManifest, JobKind, JobStatus
from coire_node.deps import JobsDep, ReservationsDep
from coire_node.jobs import InsufficientSpace, JobConflict, JobSupervisor
from coire_node.reservations import ReservationRefused

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/node/jobs", tags=["jobs"])
tracer = trace.get_tracer("coire.node.acquisition")
meter = metrics.get_meter("coire.node.acquisition")
job_starts = meter.create_counter(
    "coire_node_acquisition_jobs_total",
    unit="1",
    description="Node acquisition jobs accepted or reattached.",
)
job_expected_bytes = meter.create_histogram(
    "coire_node_acquisition_expected_bytes",
    unit="By",
    description="Expected bytes for acquisition jobs.",
)


@router.post("/reservations", response_model=Reservation, status_code=status.HTTP_201_CREATED)
async def hold_reservation(
    request: ReservationRequest, response: Response, reservations: ReservationsDep
) -> Reservation:
    try:
        item, created = reservations.hold(request)
    except ReservationRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "impossible" if exc.impossible else "waiting_for_capacity",
                "required_bytes": exc.required,
                "committed_bytes": exc.committed,
                "budget_bytes": exc.budget,
            },
        ) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return item


@router.delete("/reservations/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release_reservation(reservation_id: uuid.UUID, reservations: ReservationsDep) -> Response:
    reservations.release(reservation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    repo_id: str
    slug: str
    revision: str = "main"
    expected_total_bytes: int | None = None


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    slug: str
    source_node: str
    grant: str = Field(min_length=32)
    manifest: ChecksumManifest


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    slug: str
    manifest: ChecksumManifest | None = None


class ConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    repo_id: str
    revision: str
    source_slug: str
    target_slug: str
    reservation_id: uuid.UUID
    recipe: dict[str, object]
    dequantize: bool = False
    expected_total_bytes: int | None = None


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    slug: str
    tolerance: float = Field(default=0.1, ge=0.0, le=1.0)
    validator_version: str = "v1"
    chat_template_present: bool = False
    reference_perplexity: float | None = Field(default=None, ge=0.0)
    reference_variant_id: uuid.UUID | None = None


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    slug: str


def _start(jobs: JobSupervisor, response: Response, **kw: Any) -> JobStatus:
    kind = kw["kind"]
    with tracer.start_as_current_span(f"coire.node.acquisition.{kind.value}") as span:
        span.set_attribute("coire.job_id", str(kw["job_id"]))
        span.set_attribute("coire.model_slug", str(kw["slug"]))
        try:
            created, job_status = jobs.start(**kw)
        except JobConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except InsufficientSpace as exc:
            raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
        outcome = "created" if created else "attached"
        span.set_attribute("coire.outcome", outcome)
        job_starts.add(1, {"kind": kind.value, "outcome": outcome})
        expected = kw.get("expected_total_bytes")
        if isinstance(expected, int):
            job_expected_bytes.record(expected, {"kind": kind.value})
    # 202 means "started"; 200 means "this already existed", which is how the caller knows its
    # re-issue was a no-op rather than a second download.
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return job_status


@router.post("/pull", response_model=JobStatus)
async def start_pull(request: PullRequest, response: Response, jobs: JobsDep) -> JobStatus:
    return _start(
        jobs,
        response,
        job_id=request.job_id,
        kind=JobKind.PULL,
        slug=request.slug,
        params={"repo_id": request.repo_id, "revision": request.revision},
        expected_total_bytes=request.expected_total_bytes,
    )


@router.post("/import", response_model=JobStatus)
async def start_import(request: ImportRequest, response: Response, jobs: JobsDep) -> JobStatus:
    return _start(
        jobs,
        response,
        job_id=request.job_id,
        kind=JobKind.IMPORT,
        slug=request.slug,
        params={
            "source_node": request.source_node,
            "grant": request.grant,
            "manifest": request.manifest.model_dump(mode="json"),
        },
        expected_total_bytes=request.manifest.total_bytes,
    )


@router.post("/verify", response_model=JobStatus)
async def start_verify(request: VerifyRequest, response: Response, jobs: JobsDep) -> JobStatus:
    return _start(
        jobs,
        response,
        job_id=request.job_id,
        kind=JobKind.VERIFY,
        slug=request.slug,
        params={"manifest": request.manifest.model_dump(mode="json") if request.manifest else None},
    )


@router.post("/convert", response_model=JobStatus)
async def start_convert(
    request: ConvertRequest,
    response: Response,
    jobs: JobsDep,
    reservations: ReservationsDep,
) -> JobStatus:
    reservation = reservations.get(request.reservation_id)
    if reservation is None or reservation.state.value != "held":
        raise HTTPException(status.HTTP_409_CONFLICT, "a held conversion reservation is required")
    return _start(
        jobs,
        response,
        job_id=request.job_id,
        kind=JobKind.CONVERT,
        slug=request.target_slug,
        params={
            "repo_id": request.repo_id,
            "revision": request.revision,
            "source_slug": request.source_slug,
            "reservation_id": str(request.reservation_id),
            "recipe": request.recipe,
            "dequantize": request.dequantize,
        },
        expected_total_bytes=request.expected_total_bytes,
    )


@router.post("/validate", response_model=JobStatus)
async def start_validate(request: ValidateRequest, response: Response, jobs: JobsDep) -> JobStatus:
    return _start(
        jobs,
        response,
        job_id=request.job_id,
        kind=JobKind.VALIDATE,
        slug=request.slug,
        params={
            "tolerance": request.tolerance,
            "validator_version": request.validator_version,
            "chat_template_present": request.chat_template_present,
            "reference_perplexity": request.reference_perplexity,
            "reference_variant_id": (
                str(request.reference_variant_id) if request.reference_variant_id else None
            ),
        },
    )


@router.post("/cleanup", response_model=JobStatus)
async def start_cleanup(request: CleanupRequest, response: Response, jobs: JobsDep) -> JobStatus:
    return _start(
        jobs,
        response,
        job_id=request.job_id,
        kind=JobKind.CLEANUP,
        slug=request.slug,
        params={},
    )


@router.get("", response_model=list[JobStatus])
async def list_jobs(jobs: JobsDep) -> list[JobStatus]:
    return jobs.active()


@router.get("/{job_id}", response_model=JobStatus)
async def get_job(job_id: uuid.UUID, jobs: JobsDep) -> JobStatus:
    found = jobs.status(job_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return found


@router.delete("/{job_id}", status_code=status.HTTP_202_ACCEPTED)
async def cancel_job(job_id: uuid.UUID, jobs: JobsDep) -> Response:
    if not jobs.cancel(job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return Response(status_code=status.HTTP_202_ACCEPTED)
