"""User-owned repository registrations for MCP coding calls."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAuthenticated, PrincipalKind
from coire_api.db import RegisteredWorkspaceRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.workspaces import (
    WorkspaceSourceError,
    project_workspace,
    register_workspace,
)
from coire_core.models.mcp import RegisteredWorkspace, WorkspaceRegistrationCreate

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


def _owner(principal: CurrentAuthenticated) -> uuid.UUID:
    if principal.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "workspace requires a user identity")
    return principal.user_id


def _browser_write_origin(request: Request, principal: CurrentAuthenticated) -> None:
    if principal.kind is PrincipalKind.API_KEY:
        return
    expected = request.app.state.settings.chat_browser_origin
    if not expected or request.headers.get("origin") != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "same-origin workspace request required")


@router.post("", response_model=RegisteredWorkspace, status_code=status.HTTP_201_CREATED)
async def create_registered_workspace(
    body: WorkspaceRegistrationCreate,
    request: Request,
    principal: CurrentAuthenticated,
    session: SessionDep,
    settings: SettingsDep,
) -> RegisteredWorkspace:
    _browser_write_origin(request, principal)
    owner = _owner(principal)
    try:
        row = await register_workspace(
            session, owner_user_id=owner, request=body, settings=settings
        )
    except WorkspaceSourceError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await write_principal_audit(
        session,
        principal=principal,
        action="mcp_workspace.register",
        target_type="registered_workspace",
        target_id=str(row.id),
        detail={"repository_host": body.repository_url.host},
    )
    await session.commit()
    return project_workspace(row)


@router.get("", response_model=list[RegisteredWorkspace])
async def list_registered_workspaces(
    principal: CurrentAuthenticated, session: SessionDep
) -> list[RegisteredWorkspace]:
    owner = _owner(principal)
    rows = (
        await session.scalars(
            select(RegisteredWorkspaceRow)
            .where(RegisteredWorkspaceRow.owner_user_id == owner)
            .order_by(RegisteredWorkspaceRow.created_at.desc())
        )
    ).all()
    return [project_workspace(row) for row in rows]


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registered_workspace(
    workspace_id: uuid.UUID,
    request: Request,
    principal: CurrentAuthenticated,
    session: SessionDep,
) -> None:
    _browser_write_origin(request, principal)
    owner = _owner(principal)
    row = await session.scalar(
        select(RegisteredWorkspaceRow).where(
            RegisteredWorkspaceRow.id == workspace_id,
            RegisteredWorkspaceRow.owner_user_id == owner,
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
    await session.delete(row)
    await write_principal_audit(
        session,
        principal=principal,
        action="mcp_workspace.delete",
        target_type="registered_workspace",
        target_id=str(workspace_id),
        detail={},
    )
    await session.commit()
