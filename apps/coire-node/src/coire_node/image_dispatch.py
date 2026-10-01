"""One fenced node-to-worker dispatch after durable intent journaling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from coire_core.models.image_worker import (
    ImageJobBinding,
    ImageWorkerCancelRequest,
    ImageWorkerLoadRequest,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
    ImageWorkerUnloadRequest,
    NodeImageCancelRequest,
    NodeImageCleanupReceipt,
    NodeImageCleanupRequest,
    NodeImageInputReceipt,
    NodeImageInputRequest,
    NodeImageJob,
    NodeImageStartRequest,
    NodeImageTransferRequest,
)
from coire_core.models.images import ImageMode
from coire_node.image_cleanup import cleanup_image_outputs, discard_cancelled_image_scratch
from coire_node.image_jobs import (
    ImageJobJournal,
    ImageJournalConflict,
    discard_node_image_inputs,
    stage_node_image_input,
)
from coire_node.image_runtime.supervisor import ImageProcessSupervisor, ImageProcessUnavailable
from coire_node.image_transfer import ImageTransferUnavailable, push_image_outputs
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


def _progress(
    worker_status: ImageWorkerStatus, request: NodeImageStartRequest
) -> tuple[int | None, int | None]:
    if worker_status.step is None:
        return None, None
    if (
        worker_status.output_index is None
        or worker_status.total_steps != request.resolved.spec.steps
        or worker_status.step > request.resolved.spec.steps
        or worker_status.output_index >= request.resolved.spec.n
    ):
        raise ImageDispatchUnavailable()
    return (
        worker_status.output_index * request.resolved.spec.steps + worker_status.step,
        request.resolved.spec.n * request.resolved.spec.steps,
    )


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

    async def reserve_inputs(self, request: NodeImageStartRequest) -> tuple[bool, NodeImageJob]:
        """Persist a queued advanced attempt before any private input upload."""
        async with self._lock:
            if not request.inputs:
                raise ImageDispatchConflict()
            try:
                load, _port, _token = self.worker.private_control(request.instance_id)
            except ImageProcessUnavailable:
                raise ImageDispatchUnavailable() from None
            if (
                request.instance_id != load.instance_id
                or request.model_id != load.model_id
                or request.resolved.spec.variant_id != load.variant_id
                or request.resolved.model_sha256 != load.manifest_sha256
                or request.resolved.pipeline_version != load.runtime_version
                or request.reservation_bytes != load.reservation_bytes
            ):
                raise ImageDispatchConflict()
            existing = self.journal.get(request.job_id)
            return existing is None, self.journal.begin(request)

    async def stage_input(
        self, command: NodeImageInputRequest, chunks: AsyncIterable[bytes]
    ) -> NodeImageInputReceipt:
        async with self._lock:
            try:
                async with asyncio.timeout(4.0):
                    return await stage_node_image_input(
                        self.journal, self.worker.settings.node_state_dir, command, chunks
                    )
            except TimeoutError:
                raise ImageDispatchUnavailable() from None

    async def _worker_status(self, current: NodeImageJob) -> ImageWorkerStatus | None:
        """Return the worker attempt. None means the worker is up and has never accepted it."""
        try:
            _, port, token = self.worker.private_control(current.instance_id)
        except ImageProcessUnavailable:
            raise ImageDispatchUnavailable() from None
        binding = ImageJobBinding(
            job_id=current.job_id, attempt=current.attempt, fence=current.fence
        )
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, timeout=2.0
            ) as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/status",
                    headers={"Authorization": f"Bearer {token}"},
                    json=binding.model_dump(mode="json"),
                )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ImageDispatchUnavailable()
            observed = ImageWorkerStatus.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ImageDispatchUnavailable):
            raise ImageDispatchUnavailable() from None
        if (
            observed.job_id != current.job_id
            or observed.attempt != current.attempt
            or observed.fence != current.fence
        ):
            raise ImageDispatchUnavailable()
        return observed

    async def start(self, request: NodeImageStartRequest) -> tuple[bool, NodeImageJob]:
        """Deliver one worker start. A later journal state is never generated again."""
        async with self._lock:
            existing = self.journal.get(request.job_id)
            if existing is not None and existing.state not in {"queued", "reserving"}:
                return False, self.journal.begin(request)
            if existing is not None:
                observed = await self._worker_status(existing)
                if observed is not None:
                    original = self.journal.request(request.job_id)
                    if original is None:
                        raise ImageDispatchUnavailable()
                    return False, self._reconcile(existing, original, observed)
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
                progress_step, progress_total = _progress(worker_status, request)
                running = self.journal.advance(
                    reserving.model_copy(
                        update={
                            "state": "running",
                            "progress_step": progress_step,
                            "progress_total": progress_total,
                            "updated_at": _later(reserving),
                        }
                    )
                )
                if worker_status.state == "generated":
                    if {output.index for output in worker_status.outputs} != set(
                        range(request.resolved.spec.n)
                    ):
                        raise ImageDispatchUnavailable()
                    return True, self.journal.advance(
                        running.model_copy(
                            update={
                                "state": "transferring",
                                "outputs": worker_status.outputs,
                                "updated_at": _later(running),
                            }
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

    async def status(self, binding: ImageJobBinding) -> NodeImageJob | None:
        """Observe an exact attempt; worker loss never causes a new run."""
        async with self._lock:
            current = self.journal.get(binding.job_id)
            if current is None:
                return None
            if (current.attempt, current.fence) != (binding.attempt, binding.fence):
                raise ImageDispatchConflict()
            if current.state in {"cancelled", "failed", "succeeded"}:
                return current
            request = self.journal.request(binding.job_id)
            if request is None:
                raise ImageDispatchUnavailable()
            if current.state == "transferring" and {
                output.index for output in current.outputs
            } == set(range(request.resolved.spec.n)):
                return current
            try:
                load, port, token = self.worker.private_control(current.instance_id)
            except ImageProcessUnavailable:
                raise ImageDispatchUnavailable() from None
            if not _supported(request, load):
                raise ImageDispatchUnavailable()
            with image_node_span(ImageNodeStage.STATUS, job_id=binding.job_id):
                try:
                    async with httpx.AsyncClient(
                        transport=self.transport, trust_env=False, timeout=2.0
                    ) as client:
                        response = await client.post(
                            f"http://127.0.0.1:{port}/status",
                            headers={"Authorization": f"Bearer {token}"},
                            json=binding.model_dump(mode="json"),
                        )
                    if response.status_code != 200:
                        raise ImageDispatchUnavailable()
                    observed = ImageWorkerStatus.model_validate(response.json())
                    if (
                        observed.job_id != binding.job_id
                        or observed.attempt != binding.attempt
                        or observed.fence != binding.fence
                    ):
                        raise ImageDispatchUnavailable()
                    updated = self._reconcile(current, request, observed)
                except (
                    httpx.HTTPError,
                    ValueError,
                    ImageDispatchUnavailable,
                    ImageJournalConflict,
                ):
                    record_image_stage(
                        ImageNodeStage.STATUS, ImageNodeOutcome.FAILED, job_id=binding.job_id
                    )
                    raise ImageDispatchUnavailable() from None
                record_image_stage(
                    ImageNodeStage.STATUS, ImageNodeOutcome.SUCCEEDED, job_id=binding.job_id
                )
                return updated

    def _reconcile(
        self,
        current: NodeImageJob,
        request: NodeImageStartRequest,
        observed: ImageWorkerStatus,
    ) -> NodeImageJob:
        if observed.state == "waiting":
            if current.state not in {"queued", "reserving"}:
                raise ImageDispatchUnavailable()
            return current
        if observed.state == "running":
            if current.state == "cancelling":
                return current
            if current.state not in {"reserving", "running"}:
                raise ImageDispatchUnavailable()
            progress_step, progress_total = _progress(observed, request)
            if progress_step is None:
                progress_step, progress_total = current.progress_step, current.progress_total
            return self.journal.advance(
                current.model_copy(
                    update={
                        "state": "running",
                        "progress_step": progress_step,
                        "progress_total": progress_total,
                        "updated_at": _later(current),
                    }
                )
            )
        if observed.state == "generated":
            if {output.index for output in observed.outputs} != set(range(request.resolved.spec.n)):
                raise ImageDispatchUnavailable()
            if current.state == "transferring":
                if current.outputs:
                    if current.outputs != observed.outputs:
                        raise ImageDispatchUnavailable()
                    return current
                return self.journal.advance(
                    current.model_copy(
                        update={"outputs": observed.outputs, "updated_at": _later(current)}
                    )
                )
            if current.state == "cancelling":
                return current
            if current.state not in {"reserving", "running"}:
                raise ImageDispatchUnavailable()
            running = current
            if current.state == "reserving":
                running = self.journal.advance(
                    current.model_copy(update={"state": "running", "updated_at": _later(current)})
                )
            return self.journal.advance(
                running.model_copy(
                    update={
                        "state": "transferring",
                        "outputs": observed.outputs,
                        "updated_at": _later(running),
                    }
                )
            )
        if current.state not in {"queued", "reserving", "running", "transferring", "cancelling"}:
            raise ImageDispatchUnavailable()
        terminal = "cancelled" if observed.state == "cancelled" else "failed"
        if current.state == "queued" and terminal == "cancelled":
            pass
        elif current.state == "queued":
            raise ImageDispatchUnavailable()
        return self.journal.advance(
            current.model_copy(
                update={
                    "state": terminal,
                    "safe_error": None if terminal == "cancelled" else "generation_failed",
                    "updated_at": _later(current),
                }
            )
        )

    async def cancel(self, request: NodeImageCancelRequest) -> NodeImageJob | None:
        """Journal cancel intent, then prove worker cancellation or exact process death."""
        async with self._lock:
            current = self.journal.get(request.job_id)
            if current is None:
                return None
            if (current.attempt, current.fence) != (request.attempt, request.fence):
                raise ImageDispatchConflict()
            if current.state == "cancelled" and not current.scratch_cleaned:
                await asyncio.to_thread(
                    discard_cancelled_image_scratch,
                    self.journal,
                    self.worker.settings.node_state_dir,
                    current,
                )
                await asyncio.to_thread(
                    discard_node_image_inputs,
                    self.journal,
                    self.worker.settings.node_state_dir,
                    current,
                )
                return self.journal.advance(
                    current.model_copy(
                        update={"scratch_cleaned": True, "updated_at": _later(current)}
                    )
                )
            if current.state in {"cancelled", "failed", "succeeded"}:
                return current
            if current.state == "queued":
                with image_node_span(ImageNodeStage.CANCEL, job_id=request.job_id):
                    await asyncio.to_thread(
                        discard_cancelled_image_scratch,
                        self.journal,
                        self.worker.settings.node_state_dir,
                        current,
                    )
                    await asyncio.to_thread(
                        discard_node_image_inputs,
                        self.journal,
                        self.worker.settings.node_state_dir,
                        current,
                    )
                    cancelled = self.journal.advance(
                        current.model_copy(
                            update={
                                "state": "cancelled",
                                "scratch_cleaned": True,
                                "updated_at": _later(current),
                            }
                        )
                    )
                    record_image_stage(
                        ImageNodeStage.CANCEL, ImageNodeOutcome.CANCELLED, job_id=request.job_id
                    )
                    return cancelled
            if current.state != "cancelling":
                current = self.journal.advance(
                    current.model_copy(
                        update={"state": "cancelling", "updated_at": _later(current)}
                    )
                )
            with image_node_span(ImageNodeStage.CANCEL, job_id=request.job_id):
                try:
                    confirmed = await self._cooperative_cancel(request, current)
                    if not confirmed:
                        unloaded = await asyncio.to_thread(
                            self.worker.stop,
                            ImageWorkerUnloadRequest(
                                instance_id=current.instance_id,
                                reason="admin",
                                requested_at=datetime.now(UTC),
                            ),
                        )
                        if (
                            unloaded.instance_id != current.instance_id
                            or unloaded.state != "failed"
                            or unloaded.reserved_bytes != 0
                        ):
                            raise ImageDispatchUnavailable()
                except (ImageProcessUnavailable, ImageDispatchUnavailable):
                    record_image_stage(
                        ImageNodeStage.CANCEL, ImageNodeOutcome.FAILED, job_id=request.job_id
                    )
                    raise ImageDispatchUnavailable() from None
                await asyncio.to_thread(
                    discard_cancelled_image_scratch,
                    self.journal,
                    self.worker.settings.node_state_dir,
                    current,
                )
                await asyncio.to_thread(
                    discard_node_image_inputs,
                    self.journal,
                    self.worker.settings.node_state_dir,
                    current,
                )
                cancelled = self.journal.advance(
                    current.model_copy(
                        update={
                            "state": "cancelled",
                            "safe_error": None,
                            "scratch_cleaned": True,
                            "updated_at": _later(current),
                        }
                    )
                )
                record_image_stage(
                    ImageNodeStage.CANCEL, ImageNodeOutcome.CANCELLED, job_id=request.job_id
                )
                return cancelled

    async def cleanup(self, request: NodeImageCleanupRequest) -> NodeImageCleanupReceipt:
        async with self._lock:
            current = self.journal.get(request.job_id)
            if (
                current is None
                or current.state not in {"transferring", "succeeded"}
                or (current.attempt, current.fence) != (request.attempt, request.fence)
            ):
                raise ImageJournalConflict()
            await asyncio.to_thread(
                discard_node_image_inputs,
                self.journal,
                self.worker.settings.node_state_dir,
                current,
            )
            return await asyncio.to_thread(
                cleanup_image_outputs,
                self.journal,
                self.worker.settings.node_state_dir,
                request,
            )

    async def transfer(self, request: NodeImageTransferRequest) -> NodeImageJob:
        """Push all outputs, then durably delete scratch using verified core receipts."""
        async with self._lock:
            current = self.journal.get(request.job_id)
            original = self.journal.request(request.job_id)
            if current is None or original is None:
                raise ImageJournalConflict()
            if (
                current.node != request.node
                or current.attempt != request.attempt
                or current.fence != request.fence
            ):
                raise ImageJournalConflict()
            if current.state == "succeeded":
                return current
            if current.state != "transferring":
                raise ImageJournalConflict()
            if current.receipts:
                receipts = current.receipts
                manifests = {item.index: item for item in current.outputs}
                if len(receipts) != original.resolved.spec.n or any(
                    receipt.index not in manifests
                    or receipt.byte_count != manifests[receipt.index].byte_count
                    or receipt.sha256 != manifests[receipt.index].sha256
                    or receipt.recipe_sha256 != manifests[receipt.index].recipe_sha256
                    for receipt in receipts
                ):
                    raise ImageJournalConflict()
            else:
                receipts = await push_image_outputs(
                    Path(self.worker.settings.node_state_dir),
                    current,
                    original,
                    request,
                    self.worker.settings,
                    transport=self.transport,
                )
            await asyncio.to_thread(
                discard_node_image_inputs,
                self.journal,
                self.worker.settings.node_state_dir,
                current,
            )
            await asyncio.to_thread(
                cleanup_image_outputs,
                self.journal,
                self.worker.settings.node_state_dir,
                NodeImageCleanupRequest(
                    job_id=request.job_id,
                    attempt=request.attempt,
                    fence=request.fence,
                    node=request.node,
                    receipts=receipts,
                ),
            )
            result = self.journal.get(request.job_id)
            if result is None or result.state != "succeeded":
                raise ImageTransferUnavailable()
            return result

    async def _cooperative_cancel(
        self, request: NodeImageCancelRequest, current: NodeImageJob
    ) -> bool:
        try:
            _, port, token = self.worker.private_control(current.instance_id)
            binding = ImageWorkerCancelRequest(
                job_id=request.job_id,
                attempt=request.attempt,
                fence=request.fence,
                requested_at=request.requested_at,
            )
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, timeout=0.25
            ) as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/cancel",
                    headers={"Authorization": f"Bearer {token}"},
                    json=binding.model_dump(mode="json"),
                )
                if response.status_code not in {200, 202}:
                    return False
                observed = ImageWorkerStatus.model_validate(response.json())
                if (
                    observed.job_id != request.job_id
                    or observed.attempt != request.attempt
                    or observed.fence != request.fence
                ):
                    return False
                if observed.state == "cancelled":
                    return True
                await asyncio.sleep(0.1)
                response = await client.post(
                    f"http://127.0.0.1:{port}/status",
                    headers={"Authorization": f"Bearer {token}"},
                    json=ImageJobBinding(
                        job_id=request.job_id, attempt=request.attempt, fence=request.fence
                    ).model_dump(mode="json"),
                )
                if response.status_code != 200:
                    return False
                observed = ImageWorkerStatus.model_validate(response.json())
                return (
                    observed.job_id == request.job_id
                    and observed.attempt == request.attempt
                    and observed.fence == request.fence
                    and observed.state == "cancelled"
                )
        except (ImageProcessUnavailable, httpx.HTTPError, ValueError):
            return False
