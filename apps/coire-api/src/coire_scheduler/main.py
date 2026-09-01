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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from dbos import DBOS, SetWorkflowID
from fastapi import FastAPI
from sqlalchemy import select

from coire_api.db import (
    AcquisitionWorkflowRow,
    AgentRunRow,
    ModelInstanceRow,
    PlacementDecisionRow,
    dispose_engine,
    init_engine,
    session_scope,
)
from coire_api.telemetry import configure_telemetry
from coire_core.models.acquisition import AcquisitionState
from coire_core.models.health import ReadyResponse
from coire_core.models.instance import InstanceState
from coire_core.models.placement import PlacementState
from coire_core.models.runs import AgentRunState
from coire_core.settings import get_settings
from coire_scheduler.acquisition import acquisition_workflow
from coire_scheduler.dbos_runtime import DBOSRuntime
from coire_scheduler.instances import instance_drain_workflow, instance_launch_workflow
from coire_scheduler.placement import idle_ttl_workflow, placement_workflow
from coire_scheduler.runs import run_kill_workflow, run_workflow

SERVICE_NAME = "coire-scheduler"
__version__ = "0.1.0"
logger = logging.getLogger(__name__)


async def dispatch_queued(stop: asyncio.Event) -> None:
    settings = get_settings()
    while not stop.is_set():
        try:
            async with session_scope() as session:
                ids = list(
                    (
                        await session.execute(
                            select(AcquisitionWorkflowRow.id).where(
                                AcquisitionWorkflowRow.state.in_(
                                    [
                                        AcquisitionState.QUEUED,
                                        AcquisitionState.RUNNING,
                                        AcquisitionState.WAITING_FOR_CAPACITY,
                                    ]
                                )
                            )
                        )
                    ).scalars()
                )
            for workflow_id in ids:
                with SetWorkflowID(str(workflow_id)):
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
                                )
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
                                    ]
                                )
                            )
                        )
                    ).tuples()
                )
            for run_id, run_state in run_rows:
                if run_state is AgentRunState.KILL_REQUESTED:
                    with SetWorkflowID(f"kill-{run_id}"):
                        DBOS.start_workflow(run_kill_workflow, str(run_id))
                else:
                    with SetWorkflowID(str(run_id)):
                        DBOS.start_workflow(run_workflow, str(run_id))
        except Exception:
            logger.exception("acquisition dispatcher pass failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.acquisition_poll_interval_s)


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


def create_app() -> FastAPI:
    settings = get_settings()
    configure_telemetry(SERVICE_NAME, settings.service_version, settings.otlp_endpoint)
    runtime = DBOSRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        runtime.launch()
        stop = asyncio.Event()
        dispatcher = asyncio.create_task(dispatch_queued(stop), name="acquisition-dispatcher")
        ttl_dispatcher = asyncio.create_task(
            dispatch_idle_ttl(stop), name="placement-ttl-dispatcher"
        )
        app.state.dbos = runtime
        try:
            yield
        finally:
            stop.set()
            await dispatcher
            await ttl_dispatcher
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
