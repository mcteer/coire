"""Authenticated loopback control for one resident Studio image worker."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

import psutil
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image

from coire_core.models.image_worker import (
    ImageJobBinding,
    ImageWorkerCancelRequest,
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerOutputManifest,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
)
from coire_core.models.images import (
    ImageClassificationResult,
    ResolvedImageSpec,
    canonical_recipe_bytes,
)
from coire_node.footprint import resident_bytes
from coire_node.image_runtime.classification import (
    _unknown,
    classify_image,
    record_classifier_unavailable,
)
from coire_node.image_worker import GeneratedOutput, ImageJobCancelled, Progress, run_image_job

_MAX_RETAINED_JOBS = 32
_bearer = HTTPBearer(auto_error=False)
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
_Key = tuple[str, int, int]


class _ImagePipeline(Protocol):
    def generate(
        self, resolved: ResolvedImageSpec, on_progress: Progress
    ) -> tuple[Image.Image, ...]: ...


class _WorkerState:
    def __init__(
        self,
        load: ImageWorkerLoadRequest,
        pipeline: _ImagePipeline,
        scratch_root: Path,
        classifier_model_dir: Path | None,
        classifier_memory_bytes: int,
    ) -> None:
        self.load = load
        self.pipeline = pipeline
        self.scratch_root = scratch_root
        self.classifier_model_dir = classifier_model_dir
        self.classifier_memory_bytes = classifier_memory_bytes
        self.lock = threading.RLock()
        self.jobs: dict[_Key, tuple[ImageWorkerRunRequest, ImageWorkerStatus]] = {}
        self.cancel_events: dict[_Key, threading.Event] = {}
        self.active: _Key | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def key(binding: ImageJobBinding) -> _Key:
        return binding.job_id, binding.attempt, binding.fence

    def status(self, binding: ImageJobBinding) -> ImageWorkerStatus | None:
        with self.lock:
            entry = self.jobs.get(self.key(binding))
            return entry[1] if entry else None

    def update(self, key: _Key, **changes: object) -> None:
        with self.lock:
            request, current = self.jobs[key]
            self.jobs[key] = (
                request,
                current.model_copy(update={**changes, "updated_at": datetime.now(UTC)}),
            )

    async def execute(self, key: _Key) -> None:
        with self.lock:
            request = self.jobs[key][0]
            cancelled = self.cancel_events[key]

        def progress(index: int, step: int, total: int) -> None:
            if cancelled.is_set():
                raise ImageJobCancelled()
            self.update(
                key,
                stage="generate",
                output_index=index,
                step=step,
                total_steps=total,
                cache_status=getattr(self.pipeline, "cache_status", None),
            )

        try:
            outputs = await asyncio.to_thread(
                run_image_job,
                self.pipeline,
                self.load,
                request,
                self.scratch_root,
                progress,
                is_cancelled=cancelled.is_set,
            )
            manifests = []
            for output in outputs:
                if cancelled.is_set():
                    raise ImageJobCancelled()
                classification = None
                if self.classifier_model_dir is not None:
                    footprint = resident_bytes(os.getpid())
                    if (
                        footprint is None
                        or footprint + self.classifier_memory_bytes > self.load.reservation_bytes
                    ):
                        record_classifier_unavailable(request.job_id)
                        classification = _unknown("classifier_memory")
                    else:
                        classification = await classify_image(
                            self.classifier_model_dir,
                            output.path,
                            reservation_bytes=self.classifier_memory_bytes,
                            job_id=request.job_id,
                        )
                else:
                    record_classifier_unavailable(request.job_id)
                manifests.append(_manifest(output, classification))
            if cancelled.is_set():
                raise ImageJobCancelled()
        except Exception:
            if cancelled.is_set():
                self.update(key, state="cancelled", stage="cancelled", safe_error=None)
            else:
                self.update(key, state="failed", stage="failed", safe_error="generation_failed")
        else:
            self.update(
                key,
                state="generated",
                stage="generated",
                outputs=tuple(manifests),
                cache_status=getattr(self.pipeline, "cache_status", None),
            )
        finally:
            with self.lock:
                self.active = None


def _manifest(
    output: GeneratedOutput, classification: ImageClassificationResult | None = None
) -> ImageWorkerOutputManifest:
    return ImageWorkerOutputManifest(
        index=output.index,
        byte_count=output.encoded.byte_count,
        sha256=output.encoded.sha256,
        recipe_sha256=hashlib.sha256(canonical_recipe_bytes(output.encoded.recipe)).hexdigest(),
        classification=classification,
    )


def create_worker_app(
    load: ImageWorkerLoadRequest,
    pipeline: _ImagePipeline,
    scratch_root: Path,
    *,
    token: str,
    port: int,
    classifier_model_dir: Path | None = None,
    classifier_memory_bytes: int = 1024**3,
) -> FastAPI:
    """Build the private worker app after node preflight and native model load."""
    if len(token) < 32 or not 1 <= port <= 65535:
        raise ValueError("invalid worker control configuration")
    app = FastAPI(title="coire image worker", docs_url=None, redoc_url=None, openapi_url=None)
    state = _WorkerState(
        load, pipeline, scratch_root, classifier_model_dir, classifier_memory_bytes
    )

    async def authorize(credentials: BearerDep) -> None:
        presented = credentials.credentials if credentials else ""
        if not hmac.compare_digest(presented, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid worker token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    guard = [Depends(authorize)]

    @app.get("/health", response_model=ImageWorkerLoadResult, dependencies=guard)
    async def health() -> ImageWorkerLoadResult:
        return ImageWorkerLoadResult(
            instance_id=load.instance_id,
            state="ready",
            pid=os.getpid(),
            process_create_time=psutil.Process().create_time(),
            port=port,
            reserved_bytes=load.reservation_bytes,
        )

    @app.put("/job", response_model=ImageWorkerStatus, dependencies=guard)
    async def start_job(request: ImageWorkerRunRequest, response: Response) -> ImageWorkerStatus:
        key = state.key(request)
        with state.lock:
            existing = state.jobs.get(key)
            if existing is not None:
                if existing[0] != request:
                    raise HTTPException(status.HTTP_409_CONFLICT, "worker attempt changed")
                return existing[1]
            if (
                request.instance_id != load.instance_id
                or request.resolved.spec.model_id != load.model_id
                or request.resolved.spec.variant_id != load.variant_id
                or request.resolved.model_sha256 != load.manifest_sha256
                or request.resolved.pipeline_version != load.runtime_version
                or request.deadline_at <= datetime.now(UTC)
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "worker binding unavailable")
            if state.active is not None or len(state.jobs) >= _MAX_RETAINED_JOBS:
                raise HTTPException(status.HTTP_409_CONFLICT, "worker busy")
            current = ImageWorkerStatus(
                job_id=request.job_id,
                attempt=request.attempt,
                fence=request.fence,
                state="running",
                stage="starting",
                updated_at=datetime.now(UTC),
            )
            state.jobs[key] = (request, current)
            state.cancel_events[key] = threading.Event()
            state.active = key
            task = asyncio.create_task(state.execute(key))
            state.tasks.add(task)
            task.add_done_callback(state.tasks.discard)
        response.status_code = status.HTTP_202_ACCEPTED
        return current

    @app.post("/status", response_model=ImageWorkerStatus, dependencies=guard)
    async def job_status(binding: ImageJobBinding) -> ImageWorkerStatus:
        current = state.status(binding)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "worker attempt unavailable")
        return current

    @app.post("/cancel", response_model=ImageWorkerStatus, dependencies=guard)
    async def cancel_job(
        request: ImageWorkerCancelRequest, response: Response
    ) -> ImageWorkerStatus:
        key = state.key(request)
        with state.lock:
            existing = state.jobs.get(key)
            if existing is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "worker attempt unavailable")
            current = existing[1]
            if current.state == "running":
                state.cancel_events[key].set()
                response.status_code = status.HTTP_202_ACCEPTED
            return current

    return app


async def serve_worker(app: FastAPI, *, port: int) -> None:
    """Never bind the control API to a routable Studio interface."""
    if not 1 <= port <= 65535:
        raise ValueError("invalid worker control port")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False)
    await uvicorn.Server(config).serve()
