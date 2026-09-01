"""Admin-only durable acquisition endpoints."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from coire_api.auth import CurrentAdmin
from coire_api.db import AcquisitionWorkflowRow, ModelRow, ModelVariantRow, NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.registry import acquisition, service
from coire_api.registry.inspection import classify_inspection, estimate_weight_bytes
from coire_api.registry.placement import NoCandidate, choose_origin, replica_for
from coire_core.models.acquisition import AcquisitionRequest, AcquisitionWorkflow, VariantState

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
    existing_model = (
        await session.execute(select(ModelRow).where(ModelRow.repo_id == body.repo_id))
    ).scalar_one_or_none()
    if existing_model is not None:
        duplicate_variant = (
            await session.execute(
                select(ModelVariantRow).where(
                    ModelVariantRow.model_id == existing_model.id,
                    ModelVariantRow.name == body.variant.name,
                )
            )
        ).scalar_one_or_none()
        if duplicate_variant is not None and duplicate_variant.recipe == body.variant.model_dump(
            mode="json"
        ):
            active = (
                (
                    await session.execute(
                        select(AcquisitionWorkflowRow)
                        .where(AcquisitionWorkflowRow.variant_id == duplicate_variant.id)
                        .order_by(AcquisitionWorkflowRow.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            if active is not None and active.state.value in {
                "queued",
                "running",
                "waiting_for_capacity",
            }:
                response.status_code = status.HTTP_200_OK
                return await acquisition.projection(session, active)
    retained_workflow: AcquisitionWorkflowRow | None = None
    source_variant_slug: str | None = None
    preserve_existing_raw = False
    if existing_model is not None:
        retained_variant = (
            (
                await session.execute(
                    select(ModelVariantRow).where(
                        ModelVariantRow.model_id == existing_model.id,
                        ModelVariantRow.raw_retained.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )
        if retained_variant is None:
            retained_variant = (
                (
                    await session.execute(
                        select(ModelVariantRow).where(
                            ModelVariantRow.model_id == existing_model.id,
                            ModelVariantRow.state == VariantState.READY,
                            ModelVariantRow.validated.is_(True),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if retained_variant is None:
                raise HTTPException(
                    409,
                    {
                        "code": "variant_source_unavailable",
                        "detail": "no retained raw or verified variant is available as a source",
                        "bytes_transferred": 0,
                    },
                )
            source_variant_slug = retained_variant.slug
        else:
            preserve_existing_raw = True
        retained_workflow = (
            (
                await session.execute(
                    select(AcquisitionWorkflowRow)
                    .where(AcquisitionWorkflowRow.variant_id == retained_variant.id)
                    .order_by(AcquisitionWorkflowRow.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
    try:
        if retained_workflow is not None and retained_workflow.origin_node_id is not None:
            origin_row = await session.get(NodeRow, retained_workflow.origin_node_id)
            origin = next(
                (view for view in views if origin_row is not None and view.name == origin_row.name),
                choose_origin(views),
            )
        else:
            origin = choose_origin(views)
        replica = replica_for(origin, views)
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
    node_rows = {
        row.name: row
        for row in (
            await session.execute(
                select(NodeRow).where(NodeRow.name.in_([origin.name, replica.name]))
            )
        )
        .scalars()
        .all()
    }
    if origin.name not in node_rows or replica.name not in node_rows:
        raise HTTPException(503, "selected acquisition nodes are not registered")
    effective_body = body.model_copy(update={"keep_raw": True}) if preserve_existing_raw else body
    try:
        _, _, workflow, created = await acquisition.submit(
            session,
            effective_body,
            revision=metadata.revision,
            weight_bytes=metadata.weight_bytes,
            total_bytes=metadata.total_bytes,
            memory_estimate_bytes=int(
                estimated * settings.overhead_for(body.variant.precision.value)
            ),
            origin_node_id=node_rows[origin.name].id,
            replica_node_id=node_rows[replica.name].id,
            inspection=decision.model_dump(mode="json"),
            skip_pull=existing_model is not None,
            source_variant_slug=source_variant_slug,
            actor=actor,
        )
    except acquisition.AcquisitionError as exc:
        await session.rollback()
        raise HTTPException(
            exc.status_code,
            {"code": exc.code, "detail": exc.detail, "bytes_transferred": 0},
        ) from exc
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
