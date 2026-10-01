"""Single-conversion private file-worker HTTP service."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from opentelemetry import metrics, trace

from coire_core.models.files import (
    FileProcessCancel,
    FileProcessRequest,
    FileProcessResult,
    FileProcessStatus,
    FilePurgeResult,
    FileWorkerHealth,
    ImageFileProcessRequest,
    ImageFileProcessResult,
    is_ulid,
)
from coire_core.models.image_worker import ImageRecipeParseRequest, ImageRecipeParseResult
from coire_core.settings import Settings
from coire_file_worker.image_inputs import (
    ImageInputProcessError,
    ImageRecipeParseError,
    parse_recipe_png,
    process_image_input,
)
from coire_file_worker.processor import FileProcessingError, process_file
from coire_file_worker.security import require_service_token

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.file_worker")
processed_total = metrics.get_meter("coire.file_worker").create_counter(
    "coire_file_processing_total", unit="1", description="Private file processing outcomes"
)
image_recipe_total = metrics.get_meter("coire.file_worker").create_counter(
    "coire_image_recipe_parse_total", unit="1", description="Private image recipe parse outcomes"
)
image_inputs_total = metrics.get_meter("coire.file_worker").create_counter(
    "coire_image_input_worker_total", unit="1", description="Isolated image input outcomes"
)


@dataclass
class _Job:
    request: FileProcessRequest
    status: FileProcessStatus


class Worker:
    """Per-process immutable request ledger; the scheduler owns durability."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs: dict[str, _Job] = {}
        self.lock = asyncio.Lock()
        self.active_job: str | None = None
        self.active_task: asyncio.Task[None] | None = None
        self.recipe_active = False

    @staticmethod
    def _status(request: FileProcessRequest, state: str, **kwargs: object) -> FileProcessStatus:
        return FileProcessStatus.model_validate(
            {"job_id": request.job_id, "state": state, "updated_at": datetime.now(UTC), **kwargs}
        )

    async def submit(self, request: FileProcessRequest) -> FileProcessStatus:
        async with self.lock:
            existing = self.jobs.get(request.job_id)
            if existing is not None:
                if existing.request != request:
                    raise HTTPException(status_code=409, detail="job ID conflict")
                return existing.status
            now = datetime.now(UTC)
            if (
                request.deadline_at.tzinfo is None
                or request.deadline_at <= now
                or request.deadline_at
                > now + timedelta(seconds=self.settings.file_worker_process_timeout_s)
            ):
                raise HTTPException(status_code=422, detail="invalid deadline")
            if self.active_job is not None or self.recipe_active:
                raise HTTPException(status_code=429, detail="worker busy")
            job = _Job(request, self._status(request, "running"))
            self.jobs[request.job_id] = job
            self.active_job = request.job_id
            self.active_task = asyncio.create_task(
                self._run(job), name=f"file-worker-{request.job_id}"
            )
            return job.status

    async def _run(self, job: _Job) -> None:
        request = job.request
        outcome = "internal_failure"
        try:
            with tracer.start_as_current_span("coire.file_worker.process") as span:
                span.set_attribute("job_id", request.job_id)
                span.set_attribute("input_id", str(request.input_id))
                result = await asyncio.to_thread(self._process_with_watchdog, request)
            async with self.lock:
                if job.status.state == "cancelled":
                    self._discard(request.job_id)
                    outcome = "cancelled"
                else:
                    job.status = self._status(request, "processed", result=result)
                    outcome = "processed"
        except FileProcessingError as exc:
            async with self.lock:
                self._discard(request.job_id)
                if job.status.state == "cancelled":
                    outcome = "cancelled"
                else:
                    job.status = self._status(request, "failed", safe_error=exc.code)
                    outcome = "refused"
        except Exception as exc:
            async with self.lock:
                self._discard(request.job_id)
                if job.status.state != "cancelled":
                    job.status = self._status(request, "failed", safe_error="processing_failed")
            logger.error(
                "file processing failed job_id=%s error_type=%s", request.job_id, type(exc).__name__
            )
        finally:
            async with self.lock:
                if self.active_job == request.job_id:
                    self.active_job = None
                    self.active_task = None
            processed_total.add(1, {"outcome": outcome})
            logger.info("file processing finished job_id=%s outcome=%s", request.job_id, outcome)

    def _process_with_watchdog(self, request: FileProcessRequest) -> FileProcessResult:
        remaining = (request.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise FileProcessingError("deadline_exceeded")
        # A native PDFium/Pillow call cannot be cancelled by asyncio. The container
        # runs one process; exit forces scheduler recovery and prevents a stuck parser.
        watchdog = threading.Timer(remaining, os._exit, args=(124,))
        watchdog.daemon = True
        watchdog.start()
        try:
            return process_file(
                request,
                Path(self.settings.file_worker_input_root),
                Path(self.settings.file_worker_output_root),
            )
        finally:
            watchdog.cancel()

    def _discard(self, job_id: str) -> None:
        shutil.rmtree(Path(self.settings.file_worker_output_root) / job_id, ignore_errors=True)

    def _parse_recipe_with_watchdog(
        self, request: ImageRecipeParseRequest
    ) -> ImageRecipeParseResult:
        watchdog = threading.Timer(
            self.settings.file_worker_process_timeout_s, os._exit, args=(124,)
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            recipe = parse_recipe_png(
                Path(self.settings.file_worker_image_input_root) / str(request.input_id),
                expected_size=request.byte_count,
                expected_sha256=request.source_sha256,
            )
            return ImageRecipeParseResult(
                input_id=request.input_id,
                source_sha256=request.source_sha256,
                byte_count=request.byte_count,
                recipe=recipe,
            )
        finally:
            watchdog.cancel()

    async def parse_recipe(self, request: ImageRecipeParseRequest) -> ImageRecipeParseResult:
        async with self.lock:
            if self.active_job is not None or self.recipe_active:
                raise HTTPException(status_code=429, detail="worker busy")
            self.recipe_active = True
        task = asyncio.create_task(asyncio.to_thread(self._parse_recipe_with_watchdog, request))
        outcome = "failed"
        try:
            with tracer.start_as_current_span("coire.file_worker.image_recipe_parse"):
                result = await asyncio.shield(task)
            outcome = "parsed"
            return result
        except ImageRecipeParseError as exc:
            outcome = "refused"
            raise HTTPException(status_code=422, detail=exc.code) from exc
        except asyncio.CancelledError:
            # The parser thread cannot be interrupted; retain the one-worker reservation.
            with suppress(Exception):
                await task
            outcome = "cancelled"
            raise
        except Exception as exc:
            logger.error("image recipe parse failed error_type=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="recipe processing unavailable") from None
        finally:
            async with self.lock:
                self.recipe_active = False
            image_recipe_total.add(1, {"outcome": outcome})

    def _process_image_input_with_watchdog(
        self, request: ImageFileProcessRequest
    ) -> ImageFileProcessResult:
        remaining = (request.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0 or remaining > self.settings.file_worker_process_timeout_s:
            raise ImageInputProcessError("image_input_deadline")
        watchdog = threading.Timer(remaining, os._exit, args=(124,))
        watchdog.daemon = True
        watchdog.start()
        try:
            return process_image_input(
                request,
                Path(self.settings.file_worker_image_input_root),
                Path(self.settings.file_worker_image_output_root),
            )
        finally:
            watchdog.cancel()

    async def process_image_input(self, request: ImageFileProcessRequest) -> ImageFileProcessResult:
        async with self.lock:
            if self.active_job is not None or self.recipe_active:
                raise HTTPException(status_code=429, detail="worker busy")
            self.recipe_active = True
        task = asyncio.create_task(
            asyncio.to_thread(self._process_image_input_with_watchdog, request)
        )
        outcome = "failed"
        try:
            with tracer.start_as_current_span("coire.file_worker.image_input_process") as span:
                span.set_attribute("job_id", request.job_id)
                span.set_attribute("input_id", str(request.input_id))
                result = await asyncio.shield(task)
            outcome = "processed"
            return result
        except ImageInputProcessError as exc:
            outcome = "refused"
            raise HTTPException(status_code=422, detail=exc.code) from exc
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            outcome = "cancelled"
            raise
        except Exception as exc:
            logger.error("image input processing failed error_type=%s", type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="image input processing unavailable"
            ) from None
        finally:
            async with self.lock:
                self.recipe_active = False
            image_inputs_total.add(1, {"operation": request.operation, "outcome": outcome})

    async def get(self, job_id: str) -> FileProcessStatus:
        if not is_ulid(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        async with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            return job.status

    async def cancel(self, request: FileProcessCancel) -> FileProcessStatus:
        async with self.lock:
            job = self.jobs.get(request.job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            if job.status.state == "running":
                job.status = self._status(job.request, "cancelled")
            return job.status

    async def purge(self, job_id: str) -> FilePurgeResult:
        if not is_ulid(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        async with self.lock:
            if self.active_job == job_id:
                raise HTTPException(status_code=409, detail="job is active")
            target = Path(self.settings.file_worker_output_root) / job_id
            if target.is_symlink():
                raise HTTPException(status_code=409, detail="invalid output directory")
            try:
                await asyncio.to_thread(shutil.rmtree, target)
            except FileNotFoundError:
                pass
            except OSError:
                raise HTTPException(status_code=503, detail="output purge unavailable") from None
            self.jobs.pop(job_id, None)
            logger.info("file output purged job_id=%s", job_id)
            return FilePurgeResult(job_id=job_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    worker = Worker(settings or Settings())
    app = FastAPI(
        title="Coire private file worker", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.worker = worker

    async def authenticated(authorization: str | None = Header(default=None)) -> None:
        require_service_token(authorization, worker.settings)

    @app.get("/health", response_model=FileWorkerHealth, dependencies=[Depends(authenticated)])
    async def health() -> FileWorkerHealth:
        return FileWorkerHealth()

    @app.post(
        "/v1/process",
        response_model=FileProcessStatus,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticated)],
    )
    async def process(request: FileProcessRequest) -> FileProcessStatus:
        return await worker.submit(request)

    @app.post(
        "/v1/image-recipes/parse",
        response_model=ImageRecipeParseResult,
        dependencies=[Depends(authenticated)],
    )
    async def parse_image_recipe(request: ImageRecipeParseRequest) -> ImageRecipeParseResult:
        return await worker.parse_recipe(request)

    @app.post(
        "/v1/image-inputs/process",
        response_model=ImageFileProcessResult,
        dependencies=[Depends(authenticated)],
    )
    async def process_private_image_input(
        request: ImageFileProcessRequest,
    ) -> ImageFileProcessResult:
        return await worker.process_image_input(request)

    @app.get(
        "/v1/jobs/{job_id}", response_model=FileProcessStatus, dependencies=[Depends(authenticated)]
    )
    async def get_job(job_id: str) -> FileProcessStatus:
        return await worker.get(job_id)

    @app.post(
        "/v1/jobs/{job_id}/cancel",
        response_model=FileProcessStatus,
        dependencies=[Depends(authenticated)],
    )
    async def cancel_job(job_id: str, request: FileProcessCancel) -> FileProcessStatus:
        if job_id != request.job_id:
            raise HTTPException(status_code=409, detail="job ID conflict")
        return await worker.cancel(request)

    @app.delete(
        "/v1/jobs/{job_id}/output",
        response_model=FilePurgeResult,
        dependencies=[Depends(authenticated)],
    )
    async def purge_output(job_id: str) -> FilePurgeResult:
        return await worker.purge(job_id)

    return app


app = create_app()
