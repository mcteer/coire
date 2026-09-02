"""Execute scheduler commands from the API's existing authenticated node boundary."""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coire_api.db import AcquisitionCommandRow, AcquisitionWorkflowRow, NodeRow, session_scope
from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_core.models.acquisition import AcquisitionState, ReservationRequest, VariantRecipe
from coire_core.models.jobs import JobStage, JobStatus
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class AcquisitionCommandExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="acquisition-command-executor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                command_id = await self._next_command()
            except Exception:
                logger.exception("acquisition command queue poll failed; retrying")
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.acquisition_poll_interval_s
                    )
                continue
            if command_id is not None:
                await self._execute_safely(command_id)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.acquisition_poll_interval_s
                )

    async def _next_command(self) -> uuid.UUID | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(AcquisitionCommandRow.id)
                    .where(AcquisitionCommandRow.state.in_(["pending", "running"]))
                    .order_by(AcquisitionCommandRow.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def _execute_safely(self, command_id: uuid.UUID) -> None:
        try:
            result = await self._execute(command_id)
        except NodeError as exc:
            detail = exc.body.get("detail")
            waiting = (
                exc.kind is NodeErrorKind.CONFLICT
                and isinstance(detail, dict)
                and detail.get("code") == "waiting_for_capacity"
            )
            if exc.retryable or waiting:
                logger.warning(
                    "acquisition command %s temporarily unavailable; retaining for retry",
                    command_id,
                )
                if waiting:
                    await self._mark_waiting(command_id)
                await asyncio.sleep(self.settings.acquisition_poll_interval_s)
                return
            await self._mark_failed(command_id, exc)
            return
        except Exception as exc:
            await self._mark_failed(command_id, exc)
            return
        async with session_scope() as session:
            row = await session.get(AcquisitionCommandRow, command_id)
            if row is not None:
                row.state = "succeeded"
                row.result = result
                row.updated_at = datetime.now(UTC)

    async def _mark_waiting(self, command_id: uuid.UUID) -> None:
        async with session_scope() as session:
            command = await session.get(AcquisitionCommandRow, command_id)
            workflow = (
                await session.get(AcquisitionWorkflowRow, command.workflow_id)
                if command is not None
                else None
            )
            if workflow is not None:
                workflow.state = AcquisitionState.WAITING_FOR_CAPACITY
                workflow.updated_at = datetime.now(UTC)

    async def _mark_failed(self, command_id: uuid.UUID, exc: Exception) -> None:
        logger.exception("acquisition command %s failed", command_id, exc_info=exc)
        async with session_scope() as session:
            row = await session.get(AcquisitionCommandRow, command_id)
            if row is not None:
                row.state = "failed"
                row.failure_code = type(exc).__name__.lower()[:64]
                row.failure_detail = "node operation failed; inspect the correlated trace"
                row.updated_at = datetime.now(UTC)

    async def _execute(self, command_id: uuid.UUID) -> dict[str, object]:
        async with session_scope() as session:
            row = await session.get(AcquisitionCommandRow, command_id)
            if row is None:
                raise RuntimeError("command disappeared")
            node = await session.get(NodeRow, row.node_id)
            if node is None:
                raise RuntimeError("command node disappeared")
            row.state = "running"
            row.updated_at = datetime.now(UTC)
            operation, payload, node_name = row.operation, dict(row.payload), node.name

        async with NodeClient(self.settings, timeout=120.0) as client:
            if operation == "pull":
                status = await client.start_pull(
                    node_name,
                    job_id=command_id,
                    repo_id=str(payload["repo_id"]),
                    slug=str(payload["slug"]),
                    revision=str(payload["revision"]),
                    expected_total_bytes=int(str(payload["total_bytes"])),
                )
                return await self._wait(client, node_name, status.job_id)
            if operation == "convert":
                reservation = ReservationRequest(
                    idempotency_key=command_id,
                    workflow_id=uuid.UUID(str(payload["workflow_id"])),
                    variant_id=uuid.UUID(str(payload["variant_id"])),
                    memory_bytes=int(str(payload["memory_bytes"])),
                    disk_bytes=int(str(payload["disk_bytes"])),
                )
                await client.hold_reservation(node_name, reservation)
                try:
                    status = await client.start_convert(
                        node_name,
                        job_id=command_id,
                        repo_id=str(payload["repo_id"]),
                        revision=str(payload["revision"]),
                        source_slug=str(payload["source_slug"]),
                        target_slug=str(payload["target_slug"]),
                        reservation_id=command_id,
                        recipe=VariantRecipe.model_validate(payload["recipe"]),
                        dequantize=bool(payload.get("dequantize", False)),
                    )
                    return await self._wait(client, node_name, status.job_id)
                finally:
                    await client.release_reservation(node_name, command_id)
            if operation == "validate":
                reference_id = payload.get("reference_variant_id")
                status = await client.start_validate(
                    node_name,
                    job_id=command_id,
                    slug=str(payload["slug"]),
                    tolerance=float(str(payload["tolerance"])),
                    validator_version=str(payload["validator_version"]),
                    chat_template_present=bool(payload["chat_template_present"]),
                    reference_perplexity=(
                        float(str(payload["reference_perplexity"]))
                        if payload.get("reference_perplexity") is not None
                        else None
                    ),
                    reference_variant_id=uuid.UUID(str(reference_id)) if reference_id else None,
                )
                return await self._wait(client, node_name, status.job_id)
            if operation == "replicate":
                origin = str(payload["origin"])
                source_job = uuid.UUID(str(payload["source_job_id"]))
                source = await client.get_job(origin, source_job)
                if source.manifest is None:
                    raise RuntimeError("origin job has no manifest")
                grant = secrets.token_hex(32)
                slug = str(payload["slug"])
                await client.grant_export(
                    origin, slug, grant, datetime.now(UTC) + timedelta(hours=2)
                )
                try:
                    status = await client.start_import(
                        node_name,
                        job_id=command_id,
                        slug=slug,
                        source_node=origin,
                        grant=grant,
                        manifest=source.manifest,
                    )
                    replica = await self._wait(client, node_name, status.job_id)
                finally:
                    await client.revoke_export(origin, slug)
                return {
                    "source": source.model_dump(mode="json"),
                    "replica": replica,
                }
            if operation == "cleanup":
                status = await client.start_cleanup(
                    node_name, job_id=command_id, slug=str(payload["slug"])
                )
                return await self._wait(client, node_name, status.job_id)
        raise RuntimeError(f"unknown acquisition operation {operation}")

    async def _wait(self, client: NodeClient, node: str, job_id: uuid.UUID) -> dict[str, object]:
        # A node acknowledges job creation before its durable job record is visible to GET.
        # Treat a short initial 404 as propagation, but never mask a genuinely lost job.
        lookup_deadline = asyncio.get_running_loop().time() + 15.0
        while True:
            try:
                status: JobStatus = await client.get_job(node, job_id)
            except NodeError as exc:
                if (
                    exc.kind is NodeErrorKind.NOT_FOUND
                    and asyncio.get_running_loop().time() < lookup_deadline
                ):
                    await asyncio.sleep(self.settings.acquisition_poll_interval_s)
                    continue
                raise
            if status.stage is JobStage.DONE:
                return status.model_dump(mode="json")
            if status.stage in {JobStage.FAILED, JobStage.CANCELLED}:
                raise RuntimeError(f"node job failed: {status.error_kind}")
            await asyncio.sleep(self.settings.acquisition_poll_interval_s)
