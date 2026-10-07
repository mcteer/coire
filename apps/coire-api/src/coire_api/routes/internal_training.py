"""Private source bytes require both node identity and a live, node-bound execution grant."""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import anyio
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from coire_api.db import session_scope
from coire_api.images.transfer import require_node_credential
from coire_api.training.input_grants import authorized_input_source, open_private_source
from coire_api.training.lease_snapshots import node_lease_snapshot
from coire_core.errors import CoireError, TrainingNotFound
from coire_core.models.training_node import NodeTrainingLeaseSnapshot

router = APIRouter(prefix="/api/v1/internal/training", tags=["internal:training"])


@router.get("/nodes/{node}/leases", response_model=NodeTrainingLeaseSnapshot)
async def training_node_leases(
    node: str,
    request: Request,
    response: Response,
    x_coire_node: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> NodeTrainingLeaseSnapshot:
    if node != x_coire_node or authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(
            node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    async with session_scope() as session:
        snapshot = await node_lease_snapshot(session, node)
    response.headers["Cache-Control"] = "no-store"
    return snapshot


@router.get("/datasets/{dataset_id}/content")
async def source_content(
    dataset_id: uuid.UUID,
    request: Request,
    x_coire_node: Annotated[str, Header()],
    x_coire_dataset_grant: Annotated[str, Header()],
    authorization: Annotated[str, Header()],
) -> StreamingResponse:
    try:
        require_node_credential(
            x_coire_node, authorization.removeprefix("Bearer "), request.app.state.settings
        )
    except CoireError:
        raise TrainingNotFound() from None
    async with session_scope() as session:
        dataset = await authorized_input_source(
            session, dataset_id, x_coire_node, x_coire_dataset_grant
        )
        descriptor, byte_count = await anyio.to_thread.run_sync(
            open_private_source, request.app.state.settings, dataset
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            while block := await anyio.to_thread.run_sync(os.read, descriptor, 64 * 1024):
                yield block
        finally:
            os.close(descriptor)

    return StreamingResponse(
        body(),
        media_type="application/x-ndjson",
        headers={"Content-Length": str(byte_count), "Cache-Control": "no-store"},
    )
