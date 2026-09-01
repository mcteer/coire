"""Admin views of nodes, engines, and the audit log."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.db import AuditRow, DownloadJobRow, EngineProcessRow, NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.instance.registration import issue_token
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import ensure_ledgers
from coire_api.routes.admin_models import _engine, _job
from coire_core.models.audit import AuditAction, AuditOutcome, AuditRecord
from coire_core.models.auth import ActorType
from coire_core.models.engine import TERMINAL_ENGINE_STATES, EngineState
from coire_core.models.instance import NodeDeclaration, NodeRegistrationCredential
from coire_core.models.link import StudioDataLinkStatus
from coire_core.models.node import NodeRole, Reachability

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin: cluster"])


async def _client(settings: SettingsDep) -> AsyncGenerator[NodeClient]:
    async with NodeClient(settings) as client:
        yield client


ClientDep = Annotated[NodeClient, Depends(_client)]


@router.post(
    "/nodes", response_model=NodeRegistrationCredential, status_code=status.HTTP_201_CREATED
)
async def declare_node(
    body: NodeDeclaration,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> NodeRegistrationCredential:
    existing = await session.scalar(select(NodeRow).where(NodeRow.name == body.name))
    if existing is not None and existing.declared_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "node is already declared")
    row = existing or NodeRow(
        name=body.name,
        role=NodeRole.STUDIO,
        memory_total_bytes=body.memory_total_bytes,
        disk_total_bytes=body.disk_total_bytes,
        agent_version="unregistered",
        reachability=Reachability.UNKNOWN,
    )
    if existing is None:
        session.add(row)
        await session.flush()
    row.control_host = body.control_host
    row.data_host = body.data_host
    row.memory_total_bytes = body.memory_total_bytes
    row.disk_total_bytes = body.disk_total_bytes
    row.gpu_cores = body.gpu_cores
    row.declared_at = datetime.now(UTC)
    credential = issue_token(row, settings)
    await ensure_ledgers(
        session,
        budget_bytes=settings.placement_default_budget_bytes,
        sandbox_bytes=settings.placement_sandbox_bytes,
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="node.declare",
        target_type="node",
        target_id=str(row.id),
        detail={"name": row.name},
    )
    await session.commit()
    return credential


@router.post(
    "/nodes/{node_id}/registration-token",
    response_model=NodeRegistrationCredential,
    status_code=status.HTTP_201_CREATED,
)
async def rotate_registration_token(
    node_id: uuid.UUID,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> NodeRegistrationCredential:
    row = await session.get(NodeRow, node_id)
    if row is None or row.declared_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such declared node")
    credential = issue_token(row, settings)
    await write_principal_audit(
        session,
        principal=principal,
        action="node.registration_token.rotate",
        target_type="node",
        target_id=str(row.id),
    )
    await session.commit()
    return credential


@router.delete("/nodes/{node_id}/registration-token", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_registration_token(
    node_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> Response:
    row = await session.get(NodeRow, node_id)
    if row is None or row.declared_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such declared node")
    row.token_revoked_at = datetime.now(UTC)
    await write_principal_audit(
        session,
        principal=principal,
        action="node.registration_token.revoke",
        target_type="node",
        target_id=str(row.id),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/network/links/studios", response_model=StudioDataLinkStatus)
async def studio_data_link(principal: CurrentAdmin, client: ClientDep) -> StudioDataLinkStatus:
    try:
        return await client.data_link_status("coire-edge-a")
    except NodeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/nodes")
async def list_nodes(
    request: Request, principal: CurrentAdmin, session: SessionDep
) -> list[dict[str, Any]]:
    """Declared nodes with the last status the prober received, their engines and jobs."""
    reconciler = getattr(request.app.state, "reconciler", None)
    statuses = dict(getattr(reconciler, "node_statuses", {}) or {})
    rows = (await session.execute(select(NodeRow).order_by(NodeRow.name))).scalars().all()
    names = {r.id: r.name for r in rows}

    out: list[dict[str, Any]] = []
    for row in rows:
        status_obj = statuses.get(row.name)
        engines = (
            (
                await session.execute(
                    select(EngineProcessRow).where(EngineProcessRow.node_id == row.id)
                )
            )
            .scalars()
            .all()
        )
        jobs = (
            (
                await session.execute(
                    select(DownloadJobRow).where(
                        (DownloadJobRow.origin_node_id == row.id)
                        | (DownloadJobRow.replica_node_id == row.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        out.append(
            {
                "name": row.name,
                "role": row.role.value,
                "reachability": row.reachability.value,
                "status": _dump(status_obj),
                "engines": [_engine(e, names) for e in engines],
                "jobs": [_job(j, names) for j in jobs],
            }
        )
    return out


@router.get("/engines")
async def list_engines(principal: CurrentAdmin, session: SessionDep) -> list[dict[str, Any]]:
    names = {n.id: n.name for n in (await session.execute(select(NodeRow))).scalars().all()}
    rows = (
        (
            await session.execute(
                select(EngineProcessRow).order_by(EngineProcessRow.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_engine(r, names) for r in rows]


@router.get("/engines/{engine_id}")
async def get_engine(
    engine_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> dict[str, Any]:
    row = await _engine_row(session, engine_id)
    names = {n.id: n.name for n in (await session.execute(select(NodeRow))).scalars().all()}
    return _engine(row, names)


@router.delete("/engines/{engine_id}", status_code=status.HTTP_202_ACCEPTED)
async def unload_engine(
    engine_id: uuid.UUID,
    principal: CurrentAdmin,
    session: SessionDep,
    client: ClientDep,
) -> dict[str, Any]:
    """Unload. Works for an orphan too, which is how an operator clears one (US4 scenario 2)."""
    row = await _engine_row(session, engine_id)
    node = await session.get(NodeRow, row.node_id)
    if node is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "node is not registered")

    was_orphan = row.state is EngineState.ORPHAN
    if row.state not in TERMINAL_ENGINE_STATES:
        try:
            await client.stop_engine(node.name, engine_id)
            row.state = EngineState.STOPPING
        except NodeError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"could not reach {node.name}: {exc}"
            ) from exc

    await write_principal_audit(
        session,
        principal=principal,
        action=AuditAction.ENGINE_UNLOAD,
        target_type="engine",
        target_id=str(engine_id),
        detail={"node": node.name, "orphan": was_orphan},
    )
    await session.commit()
    return _engine(row, {node.id: node.name})


@router.get("/audit", response_model=list[AuditRecord])
async def list_audit(
    principal: CurrentAdmin,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    target_id: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    actor_type: ActorType | None = None,
    outcome: AuditOutcome | None = None,
) -> list[AuditRecord]:
    """Read the audit log. There is no route that writes, edits or deletes one."""
    query = select(AuditRow).order_by(AuditRow.at.desc()).limit(limit)
    if target_id:
        query = query.where(AuditRow.target_id == target_id)
    if action:
        query = query.where(AuditRow.action == action)
    if actor:
        query = query.where(AuditRow.actor == actor)
    if actor_type:
        query = query.where(AuditRow.actor_type == actor_type)
    if outcome:
        query = query.where(AuditRow.outcome == outcome)
    rows = (await session.execute(query)).scalars().all()
    return [AuditRecord.model_validate(r, from_attributes=True) for r in rows]


@router.get("/audit/{audit_id}", response_model=AuditRecord)
async def get_audit(
    audit_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> AuditRecord:
    row = await session.get(AuditRow, audit_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "audit record not found")
    return AuditRecord.model_validate(row, from_attributes=True)


def _dump(status_obj: Any) -> dict[str, Any] | None:
    """The prober's last NodeStatus, or null when it has none yet."""
    dump = getattr(status_obj, "model_dump", None)
    return dump(mode="json") if dump is not None else None


async def _engine_row(session: AsyncSession, engine_id: uuid.UUID) -> EngineProcessRow:
    row: EngineProcessRow | None = await session.get(EngineProcessRow, engine_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    return row
