"""Private source bytes require both node identity and a live, node-bound execution grant."""

import os
import uuid
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Annotated

import anyio
from fastapi import APIRouter, Header, HTTPException, Request, Response, Security
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from coire_api.auth import Principal
from coire_api.db import TrainingCommandRow, TrainingMeasurementRow, session_scope
from coire_api.images.transfer import require_node_credential
from coire_api.training.gateway_database import MeasurementDatabase, record_reconnect
from coire_api.training.input_grants import authorized_input_source, open_private_source
from coire_api.training.lease_snapshots import node_lease_snapshot
from coire_api.training.service import payload_digest
from coire_core.errors import CoireError, TrainingNotFound
from coire_core.models.training import (
    TrainingMeasurementCompletion,
    TrainingMeasurementGenerateRequest,
)
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


_node_credential = HTTPBearer(auto_error=False, scheme_name="NodeCredential")


@router.post(
    "/measurements/{measurement_id}/generate", response_model=TrainingMeasurementCompletion
)
async def generate_measurement(
    measurement_id: uuid.UUID,
    body: TrainingMeasurementGenerateRequest,
    request: Request,
    credential: Annotated[HTTPAuthorizationCredentials | None, Security(_node_credential)],
    x_coire_node: Annotated[str | None, Header()] = None,
) -> TrainingMeasurementCompletion:
    """Run the frozen workload through the actual gateway, never through a node shortcut."""
    started_at = perf_counter()
    settings = request.app.state.settings
    if credential is None or x_coire_node is None:
        raise HTTPException(401, "node credential required", headers={"WWW-Authenticate": "Bearer"})
    try:
        require_node_credential(x_coire_node, credential.credentials, settings)
    except CoireError:
        raise HTTPException(
            401, "node credential required", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    from coire_api.training.gateway_measurements import (
        MeasurementGateway,
        gateway_measurement_generate,
        measured_gateway_request,
    )

    generate = getattr(request.app.state, "training_measurement_gateway", None)
    if generate is None:
        generate = gateway_measurement_generate(settings)
        request.app.state.training_measurement_gateway = generate
    routing_principal = (
        generate.routing_principal(measurement_id, x_coire_node, body.principal_sha256)
        if isinstance(generate, MeasurementGateway)
        else None
    )
    database: MeasurementDatabase | None = getattr(
        request.app.state, "training_measurement_database", None
    )
    scope = database.session if database is not None else session_scope
    for read_attempt in range(2):
        async with scope() as session:
            if routing_principal is not None:
                # This is only a frozen execution identity. The callback's first
                # query freshly verifies complete document hashes and owner/key
                # state under mutation locks before any admission write.
                principal = routing_principal
                if not session.in_transaction():
                    await session.begin()
                session.info["coire.measurement.read_retry_allowed"] = True
            else:
                try:
                    record = (
                        await session.execute(
                            select(
                                TrainingMeasurementRow.state,
                                TrainingMeasurementRow.owner_user_id,
                                TrainingMeasurementRow.request["nodes"],
                                TrainingCommandRow.payload["principal"],
                            )
                            .join(
                                TrainingCommandRow,
                                (TrainingCommandRow.subject_id == str(measurement_id))
                                & (TrainingCommandRow.operation == "training.measurement"),
                            )
                            .where(TrainingMeasurementRow.id == measurement_id)
                        )
                    ).one_or_none()
                except DBAPIError as error:
                    if read_attempt != 0 or not error.connection_invalidated:
                        raise
                    await session.rollback()
                    record_reconnect(measurement_id)
                    continue
                if record is None or record[0] != "running" or x_coire_node not in record[2]:
                    raise TrainingNotFound()
                principal = Principal.model_validate(record[3])
                if (
                    principal.user_id != record[1]
                    or payload_digest(principal) != body.principal_sha256
                ):
                    raise TrainingNotFound()
            async with measured_gateway_request(request, started_at, session):
                return await generate(
                    principal, measurement_id, body.target, body.prompt, body.max_output_tokens
                )
    raise TrainingNotFound()  # Both attempts always return or raise.
