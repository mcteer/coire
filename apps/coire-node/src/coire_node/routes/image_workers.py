"""Authenticated node control of the private Studio image worker."""

from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import APIRouter, HTTPException, status

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerUnloadRequest,
)
from coire_node.deps import ImageWorkersDep
from coire_node.image_runtime.supervisor import ImageProcessUnavailable

router = APIRouter(prefix="/node/images/worker", tags=["image-worker"])


@router.put("", response_model=ImageWorkerLoadResult, status_code=status.HTTP_202_ACCEPTED)
async def load_worker(
    request: ImageWorkerLoadRequest, workers: ImageWorkersDep
) -> ImageWorkerLoadResult:
    try:
        return await asyncio.to_thread(workers.start, request)
    except ImageProcessUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image worker unavailable"
        ) from None


@router.get("/{instance_id}", response_model=ImageWorkerLoadResult)
async def get_worker(instance_id: uuid.UUID, workers: ImageWorkersDep) -> ImageWorkerLoadResult:
    current = workers.current_status()
    if current is None or current.instance_id != instance_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image worker unavailable")
    if current.state == "ready":
        return current
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            refreshed = await workers.refresh_ready(client)
            if refreshed.instance_id != instance_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "image worker unavailable")
            return refreshed
    except ImageProcessUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image worker unavailable"
        ) from None


@router.delete("/{instance_id}", response_model=ImageWorkerLoadResult)
async def unload_worker(
    instance_id: uuid.UUID, request: ImageWorkerUnloadRequest, workers: ImageWorkersDep
) -> ImageWorkerLoadResult:
    if instance_id != request.instance_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "worker instance differs from request")
    try:
        return await asyncio.to_thread(workers.stop, request)
    except ImageProcessUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image worker unavailable"
        ) from None
