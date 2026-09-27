"""Authenticated scheduler-only shard-group commands."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from coire_core.models import (
    ShardCapabilityRequest,
    ShardCapabilityResult,
    ShardGroupCommand,
    ShardGroupState,
    ShardGroupStatus,
)
from coire_core.models.gateway import EngineChatRequest
from coire_node.sharding import ShardGroupManager

router = APIRouter(prefix="/node/shard-groups", tags=["sharding"])
_proxy = httpx.AsyncClient(limits=httpx.Limits(max_connections=32, max_keepalive_connections=8))


def manager(request: Request) -> ShardGroupManager:
    value = request.app.state.shard_groups
    if not isinstance(value, ShardGroupManager):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "sharding unavailable")
    return value


@router.post("", response_model=ShardGroupStatus, status_code=status.HTTP_202_ACCEPTED)
async def prepare(command: ShardGroupCommand, request: Request) -> ShardGroupStatus:
    try:
        return manager(request).prepare(command)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "verified model copy missing") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/capabilities", response_model=ShardCapabilityResult)
async def capability(body: ShardCapabilityRequest, request: Request) -> ShardCapabilityResult:
    try:
        return manager(request).capability(body.slug, body.mode)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "verified model metadata missing") from exc


@router.get("/{group_id}", response_model=ShardGroupStatus)
async def get(group_id: uuid.UUID, request: Request) -> ShardGroupStatus:
    found = manager(request).get(group_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shard group")
    return found


@router.delete("/{group_id}", response_model=ShardGroupStatus)
async def stop(group_id: uuid.UUID, request: Request) -> ShardGroupStatus:
    found = manager(request).stop(group_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shard group")
    return found


@router.post("/{group_id}/ready", response_model=ShardGroupStatus)
async def mark_ready(group_id: uuid.UUID, request: Request) -> ShardGroupStatus:
    try:
        found = manager(request).mark_ready(group_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shard group")
    return found


@router.post("/{group_id}/proxy/v1/chat/completions")
async def proxy_group(group_id: uuid.UUID, body: EngineChatRequest, request: Request) -> Response:
    groups = manager(request)
    found = groups.get(group_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shard group")
    if found.state is not ShardGroupState.READY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "shard group is not ready")
    rank_zero = next(rank for rank in found.ranks if rank.rank == 0)
    url = f"http://{rank_zero.host}:{rank_zero.port}/v1/chat/completions"
    payload = body.model_dump(mode="json", exclude_none=True)
    expected_model = groups.model_path(group_id)
    if expected_model is None or body.model not in {expected_model, Path(expected_model).name}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model does not match shard group")
    payload["model"] = expected_model
    if not body.stream:
        try:
            upstream = await _proxy.post(url, json=payload, timeout=300)
            upstream.raise_for_status()
            return JSONResponse(status_code=upstream.status_code, content=upstream.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "shard group request failed") from exc

    async def relay() -> AsyncIterator[bytes]:
        async with _proxy.stream(
            "POST", url, json=payload, timeout=httpx.Timeout(300, read=None)
        ) as upstream:
            upstream.raise_for_status()
            async for chunk in upstream.aiter_bytes():
                yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")
