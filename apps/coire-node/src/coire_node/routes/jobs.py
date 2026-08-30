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
from pydantic import BaseModel, ConfigDict, Field

from coire_core.models.jobs import ChecksumManifest, JobKind, JobStatus
from coire_node.deps import JobsDep
from coire_node.jobs import InsufficientSpace, JobConflict, JobSupervisor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/node/jobs", tags=["jobs"])


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


def _start(jobs: JobSupervisor, response: Response, **kw: Any) -> JobStatus:
    try:
        created, job_status = jobs.start(**kw)
    except JobConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except InsufficientSpace as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc)) from exc
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
