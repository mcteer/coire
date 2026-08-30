"""Admin routes for the model registry.

Every route here depends on `CurrentAdmin`, which refuses a non-admin with 403 and an audit
row (ADR-0004). The contract test enumerates every path under `/api/v1/admin/` from the
generated OpenAPI document and asserts that, so a new route cannot quietly omit the guard.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import CurrentAdmin
from coire_api.db import DownloadJobRow, EngineProcessRow, ModelCopyRow, ModelRow, NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.registry import service
from coire_api.registry.placement import NoCandidate, choose_load_node
from coire_core.models.audit import AuditAction
from coire_core.models.engine import LIVE_ENGINE_STATES, EngineProcess, EngineState
from coire_core.models.jobs import DownloadJob
from coire_core.models.registry import (
    LoadRefusalReason,
    LoadRefused,
    Model,
    ModelAddRequest,
    ModelCopy,
    ModelState,
    ModelUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin: models"])


async def _client(settings: SettingsDep) -> AsyncGenerator[NodeClient]:
    async with NodeClient(settings) as client:
        yield client


ClientDep = Annotated[NodeClient, Depends(_client)]


def _statuses(request: Request) -> dict[str, object]:
    reconciler = getattr(request.app.state, "reconciler", None)
    return dict(getattr(reconciler, "node_statuses", {}) or {})


# --------------------------------------------------------------------------- assembly


async def _detail(session: AsyncSession, model: ModelRow) -> dict[str, object]:
    """A model with its copies, current job and engines — the admin view."""
    nodes = {n.id: n.name for n in (await session.execute(select(NodeRow))).scalars().all()}
    copies = (
        (await session.execute(select(ModelCopyRow).where(ModelCopyRow.model_id == model.id)))
        .scalars()
        .all()
    )
    job = (
        (
            await session.execute(
                select(DownloadJobRow)
                .where(DownloadJobRow.model_id == model.id)
                .order_by(DownloadJobRow.started_at.desc())
            )
        )
        .scalars()
        .first()
    )
    engines = (
        (
            await session.execute(
                select(EngineProcessRow).where(EngineProcessRow.model_id == model.id)
            )
        )
        .scalars()
        .all()
    )

    body = Model.model_validate(model, from_attributes=True).model_dump(mode="json")
    body["copies"] = [
        ModelCopy(
            node=nodes.get(c.node_id, str(c.node_id)),
            path=c.path,
            bytes=c.bytes,
            manifest_sha256=c.manifest_sha256,
            verified=c.verified,
            verified_at=c.verified_at,
            mismatched_paths=list(c.mismatched_paths or []),
            role=c.role,
        ).model_dump(mode="json")
        for c in copies
    ]
    body["job"] = _job(job, nodes) if job else None
    body["engines"] = [_engine(e, nodes) for e in engines]
    return body


def _job(row: DownloadJobRow, nodes: dict[uuid.UUID, str]) -> dict[str, object]:
    percent = (row.bytes_done / row.bytes_total * 100.0) if row.bytes_total else 0.0
    return DownloadJob(
        id=row.id,
        model_id=row.model_id,
        origin_node=nodes.get(row.origin_node_id, str(row.origin_node_id)),
        replica_node=nodes.get(row.replica_node_id, str(row.replica_node_id)),
        stage=row.stage,
        bytes_done=row.bytes_done,
        bytes_total=row.bytes_total,
        files_done=row.files_done,
        files_total=row.files_total,
        percent=min(100.0, percent),
        failure_reason=row.failure_reason,
        attempt=row.attempt,
        started_at=row.started_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    ).model_dump(mode="json")


def _engine(row: EngineProcessRow, nodes: dict[uuid.UUID, str]) -> dict[str, object]:
    return EngineProcess(
        id=row.id,
        model_id=row.model_id,
        node=nodes.get(row.node_id, str(row.node_id)),
        port=row.port,
        pid=row.pid,
        state=row.state,
        state_reason=row.state_reason,
        estimate_bytes=row.estimate_bytes,
        resident_bytes=row.resident_bytes,
        resident_delta_bytes=row.resident_delta_bytes,
        cpu_percent=row.cpu_percent,
        chat_template_sha256=row.chat_template_sha256,
        last_health_at=row.last_health_at,
        started_at=row.started_at,
        stopped_at=row.stopped_at,
    ).model_dump(mode="json")


async def _get(session: AsyncSession, model_id: uuid.UUID) -> ModelRow:
    model: ModelRow | None = await session.get(ModelRow, model_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such model")
    return model


# --------------------------------------------------------------------------- routes


@router.post("/models", status_code=status.HTTP_202_ACCEPTED)
async def add_model(
    request: ModelAddRequest,
    http_request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
) -> dict[str, object]:
    """Add a model from a Hugging Face repository and start acquiring it."""
    views = await service.node_views(session, _statuses(http_request))
    try:
        model, _job_row = await service.add_model(
            session,
            request,
            client=client,
            settings=settings,
            views=views,
            actor=principal.subject or "admin",
        )
    except service.RegistryError as exc:
        await session.commit()  # keep the refusal's audit row
        raise HTTPException(exc.status_code, exc.detail) from exc
    await session.commit()
    # Wake the reconciler so the pull starts now rather than at the next tick.
    reconciler = getattr(http_request.app.state, "reconciler", None)
    if reconciler is not None:
        reconciler._wake.set()
    return await _detail(session, model)


@router.get("/models")
async def list_models(principal: CurrentAdmin, session: SessionDep) -> list[dict[str, object]]:
    rows = (
        (await session.execute(select(ModelRow).order_by(ModelRow.created_at.desc())))
        .scalars()
        .all()
    )
    return [await _detail(session, row) for row in rows]


@router.get("/models/{model_id}")
async def get_model(
    model_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> dict[str, object]:
    return await _detail(session, await _get(session, model_id))


@router.patch("/models/{model_id}")
async def update_model(
    model_id: uuid.UUID,
    request: ModelUpdateRequest,
    principal: CurrentAdmin,
    session: SessionDep,
) -> dict[str, object]:
    model = await _get(session, model_id)
    try:
        await service.update_model(session, model, request, actor=principal.subject or "admin")
    except service.RegistryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    await session.commit()
    return await _detail(session, model)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: uuid.UUID,
    principal: CurrentAdmin,
    session: SessionDep,
    client: ClientDep,
) -> Response:
    model = await _get(session, model_id)
    try:
        await service.delete_model(
            session, model, client=client, actor=principal.subject or "admin"
        )
    except service.RegistryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/models/{model_id}/retire", status_code=status.HTTP_202_ACCEPTED)
async def retire_model(
    model_id: uuid.UUID,
    http_request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
) -> dict[str, object]:
    model = await _get(session, model_id)
    try:
        await service.retire_model(session, model, actor=principal.subject or "admin")
    except service.RegistryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    await session.commit()
    reconciler = getattr(http_request.app.state, "reconciler", None)
    if reconciler is not None:
        reconciler._wake.set()
    return await _detail(session, model)


@router.post("/models/{model_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_model(
    model_id: uuid.UUID,
    http_request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
) -> dict[str, object]:
    model = await _get(session, model_id)
    try:
        await service.retry_model(session, model, actor=principal.subject or "admin")
    except service.RegistryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    await session.commit()
    reconciler = getattr(http_request.app.state, "reconciler", None)
    if reconciler is not None:
        reconciler._wake.set()
    return await _detail(session, model)


@router.get("/models/{model_id}/job")
async def get_job(
    model_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> dict[str, object]:
    await _get(session, model_id)
    row = (
        (
            await session.execute(
                select(DownloadJobRow)
                .where(DownloadJobRow.model_id == model_id)
                .order_by(DownloadJobRow.started_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no acquisition job for this model")
    nodes = {n.id: n.name for n in (await session.execute(select(NodeRow))).scalars().all()}
    return _job(row, nodes)


@router.post("/models/{model_id}/load")
async def load_model(
    model_id: uuid.UUID,
    http_request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
    response: Response,
    body: Annotated[dict[str, str] | None, Body()] = None,
) -> dict[str, object]:
    """Load a model on a node. Exercised by tests and the console; user traffic is feature 003."""
    model = await _get(session, model_id)
    if model.state is not ModelState.READY:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            LoadRefused(
                reason=LoadRefusalReason.NOT_READY,
                message=f"{model.slug} is {model.state.value}, not ready",
            ).model_dump(mode="json"),
        )

    views = await service.node_views(session, _statuses(http_request))
    try:
        target = choose_load_node(
            model.placement_policy,
            model.memory_estimate_bytes,
            views,
            override=(body or {}).get("node"),
        )
    except NoCandidate as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            LoadRefused(reason=LoadRefusalReason.NODE_UNREACHABLE, message=str(exc)).model_dump(
                mode="json"
            ),
        ) from exc

    node = (
        await session.execute(select(NodeRow).where(NodeRow.name == target.name))
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "node is not registered")

    existing = (
        await session.execute(
            select(EngineProcessRow).where(
                EngineProcessRow.model_id == model.id,
                EngineProcessRow.node_id == node.id,
                EngineProcessRow.state.in_(list(LIVE_ENGINE_STATES)),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # FR-019: a second load of the same model on the same node is a no-op.
        response.status_code = status.HTTP_200_OK
        nodes = {node.id: node.name}
        return _engine(existing, nodes)

    engine_id = uuid.uuid4()
    row = EngineProcessRow(
        id=engine_id,
        model_id=model.id,
        node_id=node.id,
        port=0,
        state=EngineState.STARTING,
        estimate_bytes=model.memory_estimate_bytes,
    )
    session.add(row)

    try:
        already, engine_status = await client.start_engine(
            node.name,
            engine_id=engine_id,
            slug=model.slug,
            estimate_bytes=model.memory_estimate_bytes,
            chat_template=model.chat_template,
        )
    except NodeError as exc:
        await session.delete(row)
        await session.commit()
        if exc.kind.value == "conflict":
            body_out = exc.body or {}
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                LoadRefused(
                    reason=LoadRefusalReason.BUDGET,
                    message="the node refused the load: it would exceed its memory budget",
                    node=node.name,
                    required_bytes=body_out.get("required_bytes"),
                    committed_bytes=body_out.get("committed_bytes"),
                    budget_bytes=body_out.get("budget_bytes"),
                ).model_dump(mode="json"),
            ) from exc
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            LoadRefused(
                reason=LoadRefusalReason.NODE_UNREACHABLE,
                message=str(exc),
                node=node.name,
            ).model_dump(mode="json"),
        ) from exc

    row.port = engine_status.port
    row.pid = engine_status.pid
    row.process_create_time = engine_status.process_create_time
    row.state = engine_status.state
    row.chat_template_sha256 = engine_status.chat_template_sha256
    from coire_api.audit import write_audit

    await write_audit(
        session,
        actor=principal.subject or "admin",
        action=AuditAction.ENGINE_LOAD,
        target_type="engine",
        target_id=str(engine_id),
        detail={"model": model.slug, "node": node.name, "port": engine_status.port},
    )
    await session.commit()
    response.status_code = status.HTTP_200_OK if already else status.HTTP_202_ACCEPTED
    return _engine(row, {node.id: node.name})
