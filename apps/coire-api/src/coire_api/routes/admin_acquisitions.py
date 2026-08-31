"""Admin-only durable acquisition endpoints."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from coire_api.auth import CurrentAdmin
from coire_api.db import AcquisitionWorkflowRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.registry import acquisition, service
from coire_api.registry.inspection import classify_inspection, estimate_weight_bytes
from coire_api.registry.placement import NoCandidate, choose_origin
from coire_core.models.acquisition import AcquisitionRequest, AcquisitionWorkflow

router = APIRouter(prefix="/api/v1/admin", tags=["admin: acquisitions"])


async def _client(settings: SettingsDep) -> AsyncGenerator[NodeClient]:
    async with NodeClient(settings) as client:
        yield client


ClientDep = Annotated[NodeClient, Depends(_client)]


@router.post(
    "/models/acquisitions",
    response_model=AcquisitionWorkflow,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_acquisition(
    body: AcquisitionRequest,
    request: Request,
    response: Response,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
) -> AcquisitionWorkflow:
    actor = principal.subject or "admin"
    reconciler = getattr(request.app.state, "reconciler", None)
    views = await service.node_views(session, dict(getattr(reconciler, "node_statuses", {}) or {}))
    try:
        origin = choose_origin(views)
        metadata = await client.inspect(origin.name, body.repo_id, body.revision or "main")
    except NoCandidate as exc:
        raise HTTPException(503, str(exc)) from exc
    except NodeError as exc:
        code = "gated" if exc.kind.value == "gated" else "inspection_failed"
        await acquisition.reject(
            session, actor=actor, repo_id=body.repo_id, code=code, detail=exc.detail
        )
        await session.commit()
        raise HTTPException(exc.status or 502, {"code": code, "detail": exc.detail}) from exc

    decision = classify_inspection(metadata, views, settings)
    if not decision.supported:
        code = decision.rejection_code or "inspection_failed"
        detail = decision.rejection_detail or "repository cannot be acquired"
        await acquisition.reject(
            session, actor=actor, repo_id=body.repo_id, code=code, detail=detail
        )
        await session.commit()
        raise HTTPException(
            422,
            {
                "code": code,
                "detail": detail,
                "guidance": decision.source_repo_guidance,
                "fit": [item.model_dump(mode="json") for item in decision.fit],
                "bytes_transferred": 0,
            },
        )

    estimated = estimate_weight_bytes(metadata.weight_bytes, body.variant.precision)
    _, _, workflow, created = await acquisition.submit(
        session,
        body,
        revision=metadata.revision,
        weight_bytes=metadata.weight_bytes,
        total_bytes=metadata.total_bytes,
        memory_estimate_bytes=int(estimated * settings.overhead_for(body.variant.precision.value)),
        actor=actor,
    )
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return await acquisition.projection(session, workflow)


@router.get("/acquisitions/{workflow_id}", response_model=AcquisitionWorkflow)
async def get_acquisition(
    workflow_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> AcquisitionWorkflow:
    row = await session.get(AcquisitionWorkflowRow, workflow_id)
    if row is None:
        raise HTTPException(404, "no such acquisition workflow")
    return await acquisition.projection(session, row)


@router.post(
    "/acquisitions/{workflow_id}/retry",
    response_model=AcquisitionWorkflow,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_acquisition(
    workflow_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> AcquisitionWorkflow:
    row = await session.get(AcquisitionWorkflowRow, workflow_id)
    if row is None:
        raise HTTPException(404, "no such acquisition workflow")
    try:
        await acquisition.retry(session, row, actor=principal.subject or "admin")
    except acquisition.AcquisitionError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "detail": exc.detail}) from exc
    await session.commit()
    return await acquisition.projection(session, row)
