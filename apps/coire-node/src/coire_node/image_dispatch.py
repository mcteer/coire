"""One fenced node-to-worker dispatch after durable intent journaling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
    NodeImageJob,
    NodeImageStartRequest,
)
from coire_core.models.images import ImageMode
from coire_node.image_jobs import ImageJobJournal
from coire_node.image_runtime.supervisor import ImageProcessSupervisor, ImageProcessUnavailable
from coire_node.metrics import ImageNodeOutcome, ImageNodeStage, image_node_span, record_image_stage


class ImageDispatchUnavailable(RuntimeError):
    """The private worker command cannot be confirmed; never resend blindly."""


class ImageDispatchConflict(RuntimeError):
    """This command does not bind to the resident worker or supported pipeline."""


def _supported(request: NodeImageStartRequest, load: ImageWorkerLoadRequest) -> bool:
    spec = request.resolved.spec
    return (
        request.instance_id == load.instance_id
        and request.model_id == load.model_id
        and spec.variant_id == load.variant_id
        and request.resolved.model_sha256 == load.manifest_sha256
        and request.resolved.pipeline_version == load.runtime_version
        and request.reservation_bytes == load.reservation_bytes
        and spec.mode is ImageMode.TXT2IMG
        and spec.guidance == 0
        and spec.negative_prompt is None
        and not spec.loras
        and spec.init_image_id is None
        and spec.mask_id is None
        and spec.control is None
        and spec.upscale is None
        and not request.inputs
        and not request.resolved.inputs
    )


def _later(status: NodeImageJob) -> datetime:
    return max(datetime.now(UTC), status.updated_at + timedelta(microseconds=1))


class ImageNodeDispatcher:
    def __init__(
        self,
        journal: ImageJobJournal,
        worker: ImageProcessSupervisor,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.journal = journal
        self.worker = worker
        self.transport = transport
        self._lock = asyncio.Lock()

    async def start(self, request: NodeImageStartRequest) -> tuple[bool, NodeImageJob]:
        """Return (created, status); a replay never calls the worker again."""
        async with self._lock:
            existing = self.journal.get(request.job_id)
            if existing is not None:
                return False, self.journal.begin(request)
            try:
                load, port, token = self.worker.private_control(request.instance_id)
            except ImageProcessUnavailable:
                raise ImageDispatchUnavailable() from None
            if not _supported(request, load):
                raise ImageDispatchConflict()
            queued = self.journal.begin(request)
            current = self.worker.current_status()
            if current is None or current.pid is None or current.process_create_time is None:
                raise ImageDispatchUnavailable()
            reserving = self.journal.advance(
                queued.model_copy(
                    update={
                        "state": "reserving",
                        "pid": current.pid,
                        "process_create_time": current.process_create_time,
                        "updated_at": _later(queued),
                    }
                )
            )
            command = ImageWorkerRunRequest(
                job_id=request.job_id,
                attempt=request.attempt,
                fence=request.fence,
                instance_id=request.instance_id,
                resolved=request.resolved,
                inputs=request.inputs,
                deadline_at=request.deadline_at,
            )
            with image_node_span(ImageNodeStage.GENERATE, job_id=request.job_id):
                try:
                    async with httpx.AsyncClient(
                        transport=self.transport, trust_env=False, timeout=5.0
                    ) as client:
                        response = await client.put(
                            f"http://127.0.0.1:{port}/job",
                            headers={"Authorization": f"Bearer {token}"},
                            json=command.model_dump(mode="json"),
                        )
                    if response.status_code not in {200, 202}:
                        raise ImageDispatchUnavailable()
                    worker_status = ImageWorkerStatus.model_validate(response.json())
                    if (
                        worker_status.job_id != request.job_id
                        or worker_status.attempt != request.attempt
                        or worker_status.fence != request.fence
                    ):
                        raise ImageDispatchUnavailable()
                except (httpx.HTTPError, ValueError, ImageDispatchUnavailable):
                    record_image_stage(
                        ImageNodeStage.GENERATE,
                        ImageNodeOutcome.FAILED,
                        job_id=request.job_id,
                    )
                    raise ImageDispatchUnavailable() from None
            if worker_status.state == "waiting":
                return True, reserving
            if worker_status.state in {"running", "generated"}:
                running = self.journal.advance(
                    reserving.model_copy(
                        update={
                            "state": "running",
                            "progress_step": worker_status.step,
                            "progress_total": worker_status.total_steps,
                            "updated_at": _later(reserving),
                        }
                    )
                )
                if worker_status.state == "generated":
                    return True, self.journal.advance(
                        running.model_copy(
                            update={"state": "transferring", "updated_at": _later(running)}
                        )
                    )
                return True, running
            terminal = "cancelled" if worker_status.state == "cancelled" else "failed"
            return True, self.journal.advance(
                reserving.model_copy(
                    update={
                        "state": terminal,
                        "safe_error": worker_status.safe_error or "generation_failed",
                        "updated_at": _later(reserving),
                    }
                )
            )
