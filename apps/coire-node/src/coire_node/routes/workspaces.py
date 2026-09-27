"""Authenticated scheduler-to-node MCP workspace preparation routes."""

from __future__ import annotations

import uuid
from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from coire_core.models.node import (
    WorkspaceCleanupRequest,
    WorkspacePrepareRequest,
    WorkspacePrepareResult,
)
from coire_node.runs import RunManager
from coire_node.workspaces import WorkspaceError

router = APIRouter(prefix="/node/workspaces", tags=["workspaces"])


def _manager(request: Request) -> RunManager:
    manager = request.app.state.runs
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "run runtime unavailable")
    return cast(RunManager, manager)


def _translate(exc: WorkspaceError) -> HTTPException:
    code = exc.code
    http_status = (
        status.HTTP_409_CONFLICT
        if code in {"workspace_prepare_conflict", "workspace_cleanup_conflict"}
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return HTTPException(http_status, {"code": code, "detail": str(exc)})


@router.post("", response_model=WorkspacePrepareResult, status_code=status.HTTP_201_CREATED)
async def prepare_workspace(
    body: WorkspacePrepareRequest, request: Request
) -> WorkspacePrepareResult:
    try:
        return await _manager(request).workspaces.prepare(body)
    except WorkspaceError as exc:
        raise _translate(exc) from exc


@router.post("/{run_id}/cleanup", status_code=status.HTTP_204_NO_CONTENT)
async def cleanup_workspace(
    run_id: uuid.UUID, body: WorkspaceCleanupRequest, request: Request
) -> None:
    if run_id != body.run_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "run ID mismatch")
    manager = _manager(request)
    container = await manager.docker.inspect_container(manager.container_name(run_id))
    if container is not None and bool((container.get("State") or {}).get("Running")):
        raise HTTPException(status.HTTP_409_CONFLICT, "run container is still active")
    try:
        await manager.workspaces.cleanup(body)
    except WorkspaceError as exc:
        raise _translate(exc) from exc
