"""Admin views of nodes, engines, and the audit log."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import CurrentAdmin
from coire_api.db import AuditRow, DownloadJobRow, EngineProcessRow, NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.routes.admin_models import _engine, _job
from coire_core.models.audit import AuditAction, AuditRecord
from coire_core.models.engine import TERMINAL_ENGINE_STATES, EngineState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin: cluster"])


async def _client(settings: SettingsDep) -> AsyncGenerator[NodeClient]:
    async with NodeClient(settings) as client:
        yield client


ClientDep = Annotated[NodeClient, Depends(_client)]


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

    await write_audit(
        session,
        actor=principal.subject or "admin",
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
) -> list[AuditRecord]:
    """Read the audit log. There is no route that writes, edits or deletes one."""
    query = select(AuditRow).order_by(AuditRow.at.desc()).limit(limit)
    if target_id:
        query = query.where(AuditRow.target_id == target_id)
    rows = (await session.execute(query)).scalars().all()
    return [AuditRecord.model_validate(r, from_attributes=True) for r in rows]


def _dump(status_obj: Any) -> dict[str, Any] | None:
    """The prober's last NodeStatus, or null when it has none yet."""
    dump = getattr(status_obj, "model_dump", None)
    return dump(mode="json") if dump is not None else None


async def _engine_row(session: AsyncSession, engine_id: uuid.UUID) -> EngineProcessRow:
    row: EngineProcessRow | None = await session.get(EngineProcessRow, engine_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    return row
