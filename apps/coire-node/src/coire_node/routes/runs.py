"""Authenticated scheduler-to-node run lifecycle routes."""

from __future__ import annotations

import uuid
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request, status

from coire_core.models.runs import (
    RunCollectedResult,
    RunContainerCreate,
    RunContainerObservation,
    RunContainerStatus,
    RunLogChunk,
)
from coire_node.docker_api import DockerAPIError
from coire_node.runs import RunManager, RunRuntimeError

router = APIRouter(prefix="/node/runs", tags=["runs"])


def _manager(request: Request) -> RunManager:
    manager = request.app.state.runs
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "run runtime unavailable")
    return cast(RunManager, manager)


def _translate(exc: RunRuntimeError | DockerAPIError) -> HTTPException:
    if isinstance(exc, RunRuntimeError):
        code = exc.code
        http_status = (
            status.HTTP_404_NOT_FOUND
            if code in {"run_workspace_missing", "run_container_missing", "run_result_missing"}
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        return HTTPException(http_status, {"code": code, "detail": str(exc)})
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        {"code": "run_runtime_unavailable", "detail": "local container runtime failed"},
    )


@router.post("", response_model=RunContainerStatus, status_code=status.HTTP_201_CREATED)
async def create_run(command: RunContainerCreate, request: Request) -> RunContainerStatus:
    try:
        return await _manager(request).create(command)
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc


@router.post("/{run_id}/start", response_model=RunContainerStatus)
async def start_run(run_id: uuid.UUID, request: Request) -> RunContainerStatus:
    try:
        return await _manager(request).start(run_id)
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc


@router.get("/{run_id}/logs", response_model=list[RunLogChunk])
async def run_logs(
    run_id: uuid.UUID, request: Request, offset: int = Query(default=0, ge=0)
) -> list[RunLogChunk]:
    try:
        return await _manager(request).logs(run_id, offset=offset)
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc


@router.post("/{run_id}/wait", response_model=RunContainerStatus)
async def wait_run(run_id: uuid.UUID, request: Request) -> RunContainerStatus:
    try:
        return await _manager(request).wait(run_id)
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc


@router.get("/{run_id}/result", response_model=RunCollectedResult)
async def collect_run(run_id: uuid.UUID, request: Request) -> RunCollectedResult:
    try:
        return await _manager(request).collect(run_id)
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_run(
    run_id: uuid.UUID, request: Request, kill: bool = Query(default=False)
) -> None:
    try:
        await _manager(request).remove(run_id, kill=kill)
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc


@router.get("", response_model=list[RunContainerObservation])
async def list_runs(request: Request) -> list[RunContainerObservation]:
    try:
        return await _manager(request).observations()
    except (RunRuntimeError, DockerAPIError) as exc:
        raise _translate(exc) from exc
