"""Control-app-only measurement commands; attach requires node authentication."""

import asyncio
from typing import cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from coire_core.models.training_node import (
    TrainingInputsRequest,
    TrainingLeaseRenewal,
    TrainingMeasurementCapabilities,
    TrainingMeasurementNodeStatus,
    TrainingMeasurementPrepare,
    TrainingPrepared,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopReceipt,
    TrainingStopRequest,
)
from coire_core.models.training_types import TrainingId
from coire_node.routes.training import invoke, scope
from coire_node.training.measurement import MeasurementSupervisor

router = APIRouter(prefix="/node/training/measurements", tags=["training:measurements"])


def native(request: Request) -> MeasurementSupervisor:
    value = getattr(request.app.state, "training_measurements", None)
    if value is None:
        raise HTTPException(503, "reserved measurement runtime unavailable")
    return cast(MeasurementSupervisor, value)


def attach(control: FastAPI, supervisor: MeasurementSupervisor) -> None:
    control.state.training_measurements = supervisor
    control.include_router(router, dependencies=[Depends(control.state.require_node_token)])


@router.get("/capabilities", response_model=TrainingMeasurementCapabilities)
async def capabilities(request: Request) -> TrainingMeasurementCapabilities:
    result = await invoke(lambda: asyncio.to_thread(native(request).capabilities))
    if not request.app.state.settings.training_enabled:
        return result.model_copy(update={"world_sizes": []})
    return result


@router.post("/{attempt_id}/prepare", response_model=TrainingPrepared)
async def prepare(
    attempt_id: TrainingId, body: TrainingMeasurementPrepare, request: Request
) -> TrainingPrepared:
    scope(attempt_id, body.prepare, request)
    if not request.app.state.settings.training_enabled:
        raise HTTPException(503, "training is disabled")
    return await invoke(lambda: native(request).prepare_measurement(body))


@router.post("/{attempt_id}/inputs", response_model=TrainingPrepared)
async def inputs(
    attempt_id: TrainingId, body: TrainingInputsRequest, request: Request
) -> TrainingPrepared:
    scope(attempt_id, body, request)
    return await invoke(
        lambda: native(request).submit_inputs(body, settings=request.app.state.settings)
    )


@router.post("/{attempt_id}/start", response_model=TrainingStartReceipt)
async def start(
    attempt_id: TrainingId, body: TrainingStartRequest, request: Request
) -> TrainingStartReceipt:
    scope(attempt_id, body, request)
    return await invoke(lambda: native(request).start(body))


@router.post("/{attempt_id}/lease", response_model=TrainingMeasurementNodeStatus)
async def lease(
    attempt_id: TrainingId, body: TrainingLeaseRenewal, request: Request
) -> TrainingMeasurementNodeStatus:
    scope(attempt_id, body, request)
    await invoke(lambda: native(request).renew(body))
    return await invoke(lambda: asyncio.to_thread(native(request).measurement_status, attempt_id))


@router.post("/{attempt_id}/begin", status_code=204)
async def begin(attempt_id: TrainingId, request: Request) -> None:
    await invoke(lambda: asyncio.to_thread(native(request).begin_mixed, attempt_id))


@router.get("/{attempt_id}", response_model=TrainingMeasurementNodeStatus)
async def status(attempt_id: TrainingId, request: Request) -> TrainingMeasurementNodeStatus:
    return await invoke(lambda: asyncio.to_thread(native(request).measurement_status, attempt_id))


@router.post("/{attempt_id}/stop", response_model=TrainingStopReceipt)
async def stop(
    attempt_id: TrainingId, body: TrainingStopRequest, request: Request
) -> TrainingStopReceipt:
    scope(attempt_id, body, request)
    supervisor = native(request)
    receipt = await invoke(lambda: supervisor.stop(body))
    if receipt.stopped:
        supervisor.release_stopped_attempt(attempt_id)
    return receipt
