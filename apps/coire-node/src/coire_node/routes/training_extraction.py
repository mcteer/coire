"""Control-only extraction routes; AdapterExtractor.attach installs node authentication."""

from __future__ import annotations

import uuid

import anyio
from fastapi import APIRouter, HTTPException, Request

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    TrainingAdapterExtractionStatus,
    TrainingAdapterExtractRequest,
)
from coire_node.training.extraction import AdapterExtractor

router = APIRouter(prefix="/node/training/adapters/extractions")


def manager(request: Request) -> AdapterExtractor:
    value = getattr(request.app.state, "training_adapter_extractor", None)
    if not isinstance(value, AdapterExtractor):
        raise HTTPException(503, "Adapter extraction is unavailable")
    return value


@router.post("", response_model=TrainingAdapterExtractionStatus)
async def extract(
    body: TrainingAdapterExtractRequest, request: Request
) -> TrainingAdapterExtractionStatus:
    try:
        return await anyio.to_thread.run_sync(manager(request).extract, body)
    except TrainingConflict:
        raise HTTPException(409, "Extraction identity conflicts with immutable intent") from None
    except Exception:
        raise HTTPException(503, "Extraction requires reconciliation") from None


@router.get("/{command_id}", response_model=TrainingAdapterExtractionStatus)
async def status(command_id: uuid.UUID, request: Request) -> TrainingAdapterExtractionStatus:
    try:
        return await anyio.to_thread.run_sync(manager(request).status, command_id)
    except FileNotFoundError:
        raise HTTPException(404, "Extraction command is unknown") from None
    except Exception:
        raise HTTPException(503, "Extraction status requires reconciliation") from None
