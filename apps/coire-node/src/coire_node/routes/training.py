"""Authenticated control-only Studio analysis and fenced native training commands."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
import psutil
from fastapi import APIRouter, HTTPException, Query, Request
from opentelemetry import metrics, trace

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    CheckpointAcknowledgementDocument,
    CheckpointCommitAcknowledgementV2,
    NodeAnalysisCancelRequest,
    NodeDatasetAnalysisRequest,
    NodeDatasetAnalysisStatus,
    NodeTrainingEventPage,
    NodeTrainingStatus,
    TrainingAttemptCleanupReceipt,
    TrainingAttemptCleanupRequest,
    TrainingCommand,
    TrainingInputsRequest,
    TrainingLeaseRenewal,
    TrainingPauseRequest,
    TrainingPrepared,
    TrainingPrepareRequest,
    TrainingReconcileRequest,
    TrainingReconcileResult,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopReceipt,
    TrainingStopRequest,
)
from coire_core.models.training_types import TrainingId
from coire_node.reservations import ReservationLedgerUnavailable, ReservationRefused
from coire_node.training.analysis_supervisor import AnalysisSupervisor
from coire_node.training.supervisor import TrainingSupervisor

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.training")
commands = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_node_commands_total"
)


def native(request: Request, *, mutation: bool = False) -> TrainingSupervisor:
    if mutation and not request.app.state.settings.training_enabled:
        raise HTTPException(503, "training is disabled")
    value = getattr(request.app.state, "training", None)
    if value is None:
        raise HTTPException(503, "native training runtime unavailable")
    return cast(TrainingSupervisor, value)


def scope(
    attempt_id: str, command: TrainingCommand | CheckpointCommitAcknowledgementV2, request: Request
) -> None:
    if command.attempt_id != attempt_id or command.node != request.app.state.settings.node_name:
        raise HTTPException(409, "training control identity differs")
    logger.info(
        "native training control",
        extra={
            "job_id": command.job_id,
            "attempt_id": command.attempt_id,
            "node": command.node,
            "command_type": type(command).__name__,
        },
    )


async def invoke[T](operation: Callable[[], Awaitable[T]]) -> T:
    try:
        with tracer.start_as_current_span("coire.node.training.command"):
            result = await operation()
        commands.add(1, {"outcome": "succeeded"})
        return result
    except (
        TrainingConflict,
        TrainingValidationError,
        ValueError,
        OSError,
        psutil.Error,
        httpx.HTTPError,
        ReservationRefused,
        ReservationLedgerUnavailable,
    ):
        commands.add(1, {"outcome": "refused"})
        raise HTTPException(409, "training command or ownership proof unavailable") from None


router = APIRouter(prefix="/node/training", tags=["training:analysis"])


@router.post("/attempts/{attempt_id}/prepare", response_model=TrainingPrepared)
async def prepare(
    attempt_id: TrainingId, command: TrainingPrepareRequest, request: Request
) -> TrainingPrepared:
    scope(attempt_id, command, request)
    supervisor = native(request, mutation=True)
    return await invoke(lambda: supervisor.prepare(command))


@router.post("/attempts/{attempt_id}/inputs", response_model=TrainingPrepared, status_code=202)
async def inputs(
    attempt_id: TrainingId, command: TrainingInputsRequest, request: Request
) -> TrainingPrepared:
    scope(attempt_id, command, request)
    supervisor = native(request, mutation=True)
    return await invoke(
        lambda: supervisor.submit_inputs(command, settings=request.app.state.settings)
    )


@router.post("/attempts/{attempt_id}/start", response_model=TrainingStartReceipt)
async def start(
    attempt_id: TrainingId, command: TrainingStartRequest, request: Request
) -> TrainingStartReceipt:
    scope(attempt_id, command, request)
    return await invoke(lambda: native(request, mutation=True).start(command))


@router.get("/attempts/{attempt_id}", response_model=NodeTrainingStatus)
async def status_attempt(attempt_id: TrainingId, request: Request) -> NodeTrainingStatus:
    import asyncio

    return await invoke(lambda: asyncio.to_thread(native(request).observe, attempt_id))


@router.get("/attempts/{attempt_id}/events", response_model=NodeTrainingEventPage)
async def events(
    attempt_id: TrainingId, request: Request, after: int = Query(default=0, ge=0)
) -> NodeTrainingEventPage:
    import asyncio

    return await invoke(
        lambda: asyncio.to_thread(native(request).journal.events, attempt_id, after)
    )


@router.post("/attempts/{attempt_id}/lease", response_model=NodeTrainingStatus)
async def lease(
    attempt_id: TrainingId, command: TrainingLeaseRenewal, request: Request
) -> NodeTrainingStatus:
    scope(attempt_id, command, request)
    supervisor = native(request, mutation=True)
    await invoke(lambda: supervisor.renew(command))
    return supervisor.observe(attempt_id)


@router.post("/attempts/{attempt_id}/pause", response_model=NodeTrainingStatus)
async def pause(
    attempt_id: TrainingId, command: TrainingPauseRequest, request: Request
) -> NodeTrainingStatus:
    scope(attempt_id, command, request)
    supervisor = native(request)
    await invoke(lambda: supervisor.pause(command))
    return supervisor.observe(attempt_id)


@router.post("/attempts/{attempt_id}/stop", response_model=TrainingStopReceipt)
async def stop(
    attempt_id: TrainingId, command: TrainingStopRequest, request: Request
) -> TrainingStopReceipt:
    scope(attempt_id, command, request)
    supervisor = native(request)
    receipt = await invoke(lambda: supervisor.stop(command))
    if receipt.stopped:
        supervisor.release_stopped_attempt(attempt_id)
    return receipt


@router.post("/attempts/{attempt_id}/checkpoint-commit", response_model=NodeTrainingStatus)
async def checkpoint_commit(
    attempt_id: TrainingId, command: CheckpointAcknowledgementDocument, request: Request
) -> NodeTrainingStatus:
    scope(attempt_id, command, request)
    supervisor = native(request)
    await invoke(lambda: supervisor.acknowledge_checkpoint(command))
    return supervisor.observe(attempt_id)


@router.post("/reconcile", response_model=TrainingReconcileResult)
async def reconcile(command: TrainingReconcileRequest, request: Request) -> TrainingReconcileResult:
    return await invoke(lambda: native(request).reconcile(command))


@router.post("/attempts/{attempt_id}/cleanup", response_model=TrainingAttemptCleanupReceipt)
async def cleanup(
    attempt_id: TrainingId,
    command: TrainingAttemptCleanupRequest,
    request: Request,
) -> TrainingAttemptCleanupReceipt:
    import asyncio

    from coire_node.training.retention import cleanup_attempt

    if command.attempt_id != attempt_id or command.node != request.app.state.settings.node_name:
        raise HTTPException(409, "training cleanup identity differs")
    return await invoke(lambda: asyncio.to_thread(cleanup_attempt, native(request), command))


def manager(request: Request) -> AnalysisSupervisor:
    value = getattr(request.app.state, "training_analyses", None)
    if value is None:
        raise HTTPException(503, "analysis runtime unavailable")
    return cast(AnalysisSupervisor, value)


@router.post("/analyses", response_model=NodeDatasetAnalysisStatus, status_code=202)
async def start_analysis(
    command: NodeDatasetAnalysisRequest, request: Request
) -> NodeDatasetAnalysisStatus:
    if not request.app.state.settings.training_enabled:
        raise HTTPException(503, "training is disabled")
    try:
        return await manager(request).start(command)
    except (
        ValueError,
        OSError,
        httpx.HTTPError,
        TimeoutError,
        ReservationRefused,
        ReservationLedgerUnavailable,
    ):
        raise HTTPException(409, "analysis command refused") from None


@router.get("/analyses/{analysis_id}", response_model=NodeDatasetAnalysisStatus)
async def analysis_status(analysis_id: uuid.UUID, request: Request) -> NodeDatasetAnalysisStatus:
    try:
        return await manager(request).status(analysis_id)
    except FileNotFoundError:
        raise HTTPException(404, "analysis unavailable") from None
    except (ValueError, OSError, psutil.Error):
        raise HTTPException(409, "analysis identity or liveness unresolved") from None


@router.post("/analyses/{analysis_id}/cancel", response_model=NodeDatasetAnalysisStatus)
async def cancel_analysis(
    analysis_id: uuid.UUID, command: NodeAnalysisCancelRequest, request: Request
) -> NodeDatasetAnalysisStatus:
    if command.analysis_id != analysis_id:
        raise HTTPException(409, "analysis control identity differs")
    try:
        return await manager(request).cancel(analysis_id)
    except FileNotFoundError:
        raise HTTPException(404, "analysis unavailable") from None
    except (ValueError, OSError, psutil.Error):
        raise HTTPException(409, "analysis stop proof unavailable") from None
