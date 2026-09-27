"""Owner-scoped registered Git sources for MCP coding runs."""

from __future__ import annotations

import ipaddress
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import RegisteredWorkspaceRow
from coire_core.models.mcp import (
    RegisteredWorkspace,
    WorkspaceRegistrationCreate,
    WorkspaceSource,
)
from coire_core.settings import Settings


class WorkspaceSourceError(ValueError):
    pass


def validate_repository_url(source: WorkspaceSource, settings: Settings) -> None:
    url = source.repository_url
    if url is None:
        raise WorkspaceSourceError("repository source is unresolved")
    host = (url.host or "").lower().rstrip(".")
    allowed = {part.strip().lower() for part in settings.mcp_source_hosts.split(",")}
    if host not in allowed or url.port not in {None, 443}:
        raise WorkspaceSourceError("repository host is not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WorkspaceSourceError("literal IP repository hosts are denied")
    if url.query or url.fragment or url.username or url.password:
        raise WorkspaceSourceError("repository URL has forbidden parts")


async def register_workspace(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    request: WorkspaceRegistrationCreate,
    settings: Settings,
) -> RegisteredWorkspaceRow:
    validate_repository_url(
        WorkspaceSource(repository_url=request.repository_url, revision="HEAD"), settings
    )
    row = RegisteredWorkspaceRow(
        owner_user_id=owner_user_id, repository_url=str(request.repository_url)
    )
    session.add(row)
    await session.flush()
    return row


async def resolve_source(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    source: WorkspaceSource,
    settings: Settings,
) -> WorkspaceSource:
    if source.workspace_id is None:
        validate_repository_url(source, settings)
        return source
    row = await session.scalar(
        select(RegisteredWorkspaceRow).where(
            RegisteredWorkspaceRow.id == source.workspace_id,
            RegisteredWorkspaceRow.owner_user_id == owner_user_id,
        )
    )
    if row is None:
        raise WorkspaceSourceError("registered workspace is not available to caller")
    resolved = WorkspaceSource(repository_url=row.repository_url, revision=source.revision)  # type: ignore[arg-type]
    validate_repository_url(resolved, settings)
    return resolved


def project_workspace(row: RegisteredWorkspaceRow) -> RegisteredWorkspace:
    return RegisteredWorkspace(
        id=row.id,
        owner_user_id=row.owner_user_id,
        repository_url=row.repository_url,  # type: ignore[arg-type]
        created_at=row.created_at,
    )
