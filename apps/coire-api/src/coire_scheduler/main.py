"""Durable acquisition and placement scheduler.

Feature 000 ships only `/ready`. The placement scheduler, memory ledger and auto-unload are
feature 004; durable job workflows arrive with feature 002. Separating it from request handling
now means a future scheduler restart never interrupts a streaming response.

This is the only service attached to `coire-docker`, and it reaches the Docker socket solely
through the allowlisted proxy (FR-007) — never directly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import uvicorn
from dbos import DBOS, SetWorkflowID
from fastapi import FastAPI
from opentelemetry import metrics
from sqlalchemy import literal, select, union_all

from coire_api.db import (
    AcquisitionWorkflowRow,
    AgentRunRow,
    ChatFileProcessingRow,
    ImageInputRow,
    ImageJobRow,
    ModelInstanceRow,
    PlacementDecisionRow,
    RunCommandRow,
    dispose_engine,
    init_engine,
    session_scope,
)
from coire_api.polling import FailureSummary, PollBackoff, wait_or_stop
from coire_api.telemetry import configure_telemetry
from coire_core.models.acquisition import AcquisitionState
from coire_core.models.health import ReadyResponse
from coire_core.models.instance import InstanceState
from coire_core.models.placement import PlacementState
from coire_core.models.runs import AgentRunState, RunCommandState, RunOperation
from coire_core.settings import get_settings
from coire_scheduler.acquisition import acquisition_workflow
from coire_scheduler.dbos_runtime import DBOSRuntime
from coire_scheduler.files import (
    file_processing_workflow,
    purge_deleted_file_outputs,
    purge_expired_temporary_outputs,
    purge_failed_file_outputs,
)
from coire_scheduler.image_inputs import image_normalize_workflow, image_recipe_workflow
from coire_scheduler.image_latency import monitor_image_latency
from coire_scheduler.images import (
    image_cancel_workflow,
    image_dispatch_workflow,
    image_observe_workflow,
    image_publish_workflow,
    image_queue_expiry_workflow,
    image_transfer_workflow,
)
from coire_scheduler.instances import instance_drain_workflow, instance_launch_workflow
from coire_scheduler.mcp_cleanup import sweep_mcp_workspaces
from coire_scheduler.placement import idle_ttl_workflow, placement_workflow
from coire_scheduler.runs import run_kill_workflow, run_workflow
from coire_scheduler.workers import SchedulerWorkers

SERVICE_NAME = "coire-scheduler"
__version__ = "0.1.0"
logger = logging.getLogger(__name__)
kill_scan_failures = metrics.get_meter("coire.scheduler.dispatch").create_counter(
    "coire_scheduler_kill_scan_failures_total", unit="1"
)


def acquisition_dispatch_id(workflow_id: uuid.UUID, attempt: int) -> str:
    """Give each explicit retry a fresh DBOS execution while preserving restart recovery."""
    return str(workflow_id) if attempt == 1 else f"{workflow_id}:attempt:{attempt}"


async def dispatch_queued(stop: asyncio.Event) -> None:
    settings = get_settings()
    backoff = PollBackoff(
        settings.acquisition_poll_interval_s,
        # Instance drains are submitted by the API and must be discovered promptly even
        # after an idle period; a five-second idle scan plus command queue delay can miss
        # the existing ten-second drain bound on a busy runner.
        min(settings.scheduler_idle_scan_max_s, settings.acquisition_poll_interval_s),
        settings.scheduler_failure_backoff_max_s,
    )
    while not stop.is_set():
        try:
            async with session_scope() as session:
                ids = list(
                    (
                        await session.execute(
                            select(AcquisitionWorkflowRow.id, AcquisitionWorkflowRow.attempt).where(
                                AcquisitionWorkflowRow.state.in_(
                                    [
                                        AcquisitionState.QUEUED,
                                        AcquisitionState.RUNNING,
                                        AcquisitionState.WAITING_FOR_CAPACITY,
                                    ]
                                )
                            )
                        )
                    ).all()
                )
            for workflow_id, attempt in ids:
                with SetWorkflowID(acquisition_dispatch_id(workflow_id, attempt)):
                    DBOS.start_workflow(acquisition_workflow, str(workflow_id))
            async with session_scope() as session:
                placement_ids = list(
                    (
                        await session.execute(
                            select(PlacementDecisionRow.id).where(
                                PlacementDecisionRow.state.in_(
                                    [
                                        PlacementState.REQUESTED,
                                        PlacementState.WAITING_FOR_DRAIN,
                                        PlacementState.EVICTING,
                                        PlacementState.RESERVING,
                                        PlacementState.LOADING,
                                    ]
                                ),
                                PlacementDecisionRow.policy.notin_(["idle-ttl", "instance-drain"]),
                            )
                        )
                    ).scalars()
                )
            for decision_id in placement_ids:
                with SetWorkflowID(f"placement-{decision_id}"):
                    DBOS.start_workflow(placement_workflow, str(decision_id))
            async with session_scope() as session:
                instance_rows = list(
                    (
                        await session.execute(
                            select(ModelInstanceRow.id, ModelInstanceRow.state).where(
                                ModelInstanceRow.state.in_(
                                    [
                                        InstanceState.REQUESTED,
                                        InstanceState.RESERVING,
                                        InstanceState.LAUNCHING,
                                        InstanceState.WARMING,
                                        InstanceState.DRAINING,
                                    ]
                                ),
                                ModelInstanceRow.policy.notlike("image:%"),
                            )
                        )
                    ).tuples()
                )
            for instance_id, instance_state in instance_rows:
                if instance_state is InstanceState.DRAINING:
                    with SetWorkflowID(f"instance-drain-{instance_id}"):
                        DBOS.start_workflow(instance_drain_workflow, str(instance_id))
                else:
                    with SetWorkflowID(f"instance-{instance_id}"):
                        DBOS.start_workflow(instance_launch_workflow, str(instance_id))
            async with session_scope() as session:
                run_rows = list(
                    (
                        await session.execute(
                            select(AgentRunRow.id, AgentRunRow.state).where(
                                AgentRunRow.state.notin_(
                                    [
                                        AgentRunState.SUCCEEDED,
                                        AgentRunState.FAILED,
                                        AgentRunState.RESULT_COLLECTION_FAILED,
                                        AgentRunState.TIMED_OUT,
                                        AgentRunState.KILLED,
                                        AgentRunState.KILL_REQUESTED,
                                    ]
                                )
                            )
                        )
                    ).tuples()
                )
            for run_id, _run_state in run_rows:
                with SetWorkflowID(str(run_id)):
                    DBOS.start_workflow(run_workflow, str(run_id))
            async with session_scope() as session:
                file_ids = list(
                    (
                        await session.execute(
                            select(ChatFileProcessingRow.id)
                            .where(ChatFileProcessingRow.state.in_(["queued", "running"]))
                            .order_by(ChatFileProcessingRow.created_at, ChatFileProcessingRow.id)
                            .limit(1)
                        )
                    ).scalars()
                )
            for file_job_id in file_ids:
                with SetWorkflowID(f"file-{file_job_id}"):
                    DBOS.start_workflow(file_processing_workflow, file_job_id)
            async with session_scope() as session:
                image_rows = list(
                    (
                        await session.execute(
                            select(
                                ImageInputRow.id,
                                ImageInputRow.processing_job_id,
                                ImageInputRow.purpose,
                            )
                            .where(
                                ImageInputRow.state == "processing",
                                ImageInputRow.purpose.in_(["recipe", "init", "mask", "control"]),
                                ImageInputRow.processing_job_id.is_not(None),
                                ImageInputRow.deleted_at.is_(None),
                            )
                            .order_by(ImageInputRow.created_at, ImageInputRow.id)
                            .limit(1)
                        )
                    ).tuples()
                )
            for image_input_id, processing_job_id, purpose in image_rows:
                with SetWorkflowID(f"image-input-{processing_job_id}"):
                    DBOS.start_workflow(
                        image_recipe_workflow if purpose == "recipe" else image_normalize_workflow,
                        str(image_input_id),
                    )
            async with session_scope() as session:
                expired_image_ids = list(
                    (
                        await session.execute(
                            select(ImageJobRow.id)
                            .where(
                                ImageJobRow.state == "queued",
                                ImageJobRow.deadline_at <= datetime.now(UTC),
                            )
                            .order_by(ImageJobRow.deadline_at, ImageJobRow.id)
                            .limit(4)
                        )
                    ).scalars()
                )
            for image_job_id in expired_image_ids:
                with SetWorkflowID(f"image-queue-expire-{image_job_id}"):
                    DBOS.start_workflow(image_queue_expiry_workflow, image_job_id)
            async with session_scope() as session:
                dispatch_image_ids = list(
                    (
                        await session.execute(
                            select(ImageJobRow.id)
                            .where(
                                ImageJobRow.state == "queued",
                                ImageJobRow.fence == 0,
                                ImageJobRow.selected_node_id.is_(None),
                                ImageJobRow.cancel_requested_at.is_(None),
                                ImageJobRow.deadline_at > datetime.now(UTC),
                            )
                            .order_by(ImageJobRow.queued_at, ImageJobRow.id)
                            .limit(4)
                        )
                    ).scalars()
                )
            for image_job_id in dispatch_image_ids:
                with SetWorkflowID(f"image-dispatch-{image_job_id}"):
                    DBOS.start_workflow(image_dispatch_workflow, image_job_id)
            async with session_scope() as session:
                observing_image_ids = list(
                    (
                        await session.execute(
                            select(ImageJobRow.id)
                            .where(ImageJobRow.state.in_(["reserving", "running"]))
                            .order_by(ImageJobRow.updated_at, ImageJobRow.id)
                            .limit(4)
                        )
                    ).scalars()
                )
            for image_job_id in observing_image_ids:
                with SetWorkflowID(f"image-observe-{image_job_id}"):
                    DBOS.start_workflow(image_observe_workflow, image_job_id)
            async with session_scope() as session:
                image_job_ids = list(
                    (
                        await session.execute(
                            select(ImageJobRow.id)
                            .where(
                                ImageJobRow.state == "transferring",
                                ImageJobRow.cleanup_state != "cleaned",
                            )
                            .order_by(ImageJobRow.updated_at, ImageJobRow.id)
                            .limit(4)
                        )
                    ).scalars()
                )
            for image_job_id in image_job_ids:
                with SetWorkflowID(f"image-transfer-{image_job_id}"):
                    DBOS.start_workflow(image_transfer_workflow, image_job_id)
            async with session_scope() as session:
                publishing_image_ids = list(
                    (
                        await session.execute(
                            select(ImageJobRow.id)
                            .where(
                                ImageJobRow.state == "transferring",
                                ImageJobRow.receipt_state == "complete",
                                ImageJobRow.cleanup_state == "cleaned",
                            )
                            .order_by(ImageJobRow.updated_at, ImageJobRow.id)
                            .limit(4)
                        )
                    ).scalars()
                )
            for image_job_id in publishing_image_ids:
                with SetWorkflowID(f"image-publish-{image_job_id}"):
                    DBOS.start_workflow(image_publish_workflow, image_job_id)
            async with session_scope() as session:
                cancelling_image_ids = list(
                    (
                        await session.execute(
                            select(ImageJobRow.id)
                            .where(ImageJobRow.state == "cancelling")
                            .order_by(ImageJobRow.updated_at, ImageJobRow.id)
                            .limit(4)
                        )
                    ).scalars()
                )
            for image_job_id in cancelling_image_ids:
                with SetWorkflowID(f"image-cancel-{image_job_id}"):
                    DBOS.start_workflow(image_cancel_workflow, image_job_id)
            delay = (
                settings.acquisition_poll_interval_s
                if ids
                or placement_ids
                or instance_rows
                or run_rows
                or file_ids
                or image_rows
                or expired_image_ids
                or dispatch_image_ids
                or observing_image_ids
                or image_job_ids
                or publishing_image_ids
                or cancelling_image_ids
                else backoff.idle()
            )
            if (
                ids
                or placement_ids
                or instance_rows
                or run_rows
                or file_ids
                or image_rows
                or expired_image_ids
                or dispatch_image_ids
                or observing_image_ids
                or image_job_ids
                or publishing_image_ids
                or cancelling_image_ids
            ):
                backoff.active()
        except Exception:
            logger.exception("acquisition dispatcher pass failed")
            delay = backoff.failed()
        await wait_or_stop(stop, delay)


async def dispatch_kills(stop: asyncio.Event, workers: SchedulerWorkers) -> None:
    """One safety scan feeds both durable kill workflows and the independent node lane."""
    settings = get_settings()
    failures = FailureSummary()
    while not stop.is_set():
        try:
            run_query = select(
                AgentRunRow.id.label("id"),
                AgentRunRow.node_id.label("node_id"),
                literal("run").label("kind"),
            ).where(AgentRunRow.state == AgentRunState.KILL_REQUESTED)
            command_query = select(
                RunCommandRow.id.label("id"),
                RunCommandRow.node_id.label("node_id"),
                literal("command").label("kind"),
            ).where(
                RunCommandRow.operation == RunOperation.KILL,
                RunCommandRow.state.in_([RunCommandState.PENDING, RunCommandState.RUNNING]),
            )
            async with session_scope() as session:
                rows = (await session.execute(union_all(run_query, command_query))).all()
            commands: list[tuple[uuid.UUID, uuid.UUID | None]] = []
            for identifier, node_id, kind in rows:
                if kind == "run":
                    with SetWorkflowID(f"kill-{identifier}"):
                        DBOS.start_workflow(run_kill_workflow, str(identifier))
                else:
                    commands.append((identifier, node_id))
            workers.kill_executor.enqueue_kills(commands)
            failures.succeeded()
        except Exception:
            kill_scan_failures.add(1)
            suppressed = failures.failed()
            if suppressed is not None:
                logger.exception("run kill dispatcher pass failed suppressed=%d", suppressed)
        await wait_or_stop(stop, settings.run_kill_poll_interval_s)


async def dispatch_idle_ttl(stop: asyncio.Event) -> None:
    settings = get_settings()
    while not stop.is_set():
        bucket = int(asyncio.get_running_loop().time() // settings.placement_ttl_interval_s)
        try:
            with SetWorkflowID(f"placement-idle-ttl-{bucket}"):
                DBOS.start_workflow(idle_ttl_workflow)
        except Exception:
            logger.exception("placement idle-TTL dispatcher pass failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.placement_ttl_interval_s)


async def dispatch_mcp_cleanup(stop: asyncio.Event) -> None:
    settings = get_settings()
    while not stop.is_set():
        try:
            await sweep_mcp_workspaces(settings)
        except Exception:
            logger.exception("MCP cleanup pass failed")
        await wait_or_stop(stop, 300.0)


async def dispatch_file_purge(stop: asyncio.Event) -> None:
    settings = get_settings()
    while not stop.is_set():
        try:
            await purge_deleted_file_outputs(settings)
            await purge_failed_file_outputs(settings)
            await purge_expired_temporary_outputs(settings)
        except Exception as exc:
            logger.error("file purge pass failed error_type=%s", type(exc).__name__)
        await wait_or_stop(stop, 5.0)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_telemetry(SERVICE_NAME, settings.service_version, settings.otlp_endpoint)
    runtime = DBOSRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        workers = SchedulerWorkers(settings)
        stop = asyncio.Event()
        background: list[asyncio.Task[None]] = []
        try:
            runtime.launch()
            await workers.start()
            background.append(
                asyncio.create_task(dispatch_queued(stop), name="acquisition-dispatcher")
            )
            background.append(
                asyncio.create_task(dispatch_kills(stop, workers), name="kill-dispatcher")
            )
            background.append(
                asyncio.create_task(dispatch_idle_ttl(stop), name="placement-ttl-dispatcher")
            )
            background.append(
                asyncio.create_task(dispatch_mcp_cleanup(stop), name="mcp-cleanup-dispatcher")
            )
            background.append(
                asyncio.create_task(dispatch_file_purge(stop), name="file-purge-dispatcher")
            )
            background.append(
                asyncio.create_task(monitor_image_latency(stop), name="image-latency-monitor")
            )
            app.state.dbos = runtime
            yield
        finally:
            stop.set()
            if background:
                _, pending = await asyncio.wait(
                    background, timeout=settings.scheduler_shutdown_timeout_s
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            await workers.stop()
            runtime.destroy()
            await dispose_engine()

    app = FastAPI(
        title="Coire scheduler",
        version=__version__,
        docs_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/ready", response_model=ReadyResponse)
    async def get_ready() -> ReadyResponse:
        if not runtime.launched:
            from fastapi import HTTPException

            raise HTTPException(503, "durable workflow runtime is not ready")
        return ReadyResponse(service=SERVICE_NAME, version=__version__)

    return app


def main() -> None:
    # container-internal only; nothing is published from this service
    uvicorn.run(create_app(), host="0.0.0.0", port=8002, access_log=False)


if __name__ == "__main__":
    main()
