"""Owner-scoped streaming downloads for Studio branch bundles."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from opentelemetry import metrics, trace

from coire_api.auth import CurrentAuthenticated
from coire_api.db import McpArtifactRow
from coire_api.deps import SessionDep
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.mcp import BranchArtifact

router = APIRouter(prefix="/api/v1/mcp/artifacts", tags=["mcp: artifacts"])
logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.mcp.artifacts")
downloads_total = metrics.get_meter("coire.api.mcp.artifacts").create_counter(
    "coire_mcp_artifact_downloads_total", unit="1"
)


async def _owned_artifact(
    artifact_id: uuid.UUID,
    principal: CurrentAuthenticated,
    session: SessionDep,
) -> McpArtifactRow:
    row = await session.get(McpArtifactRow, artifact_id)
    if row is None or principal.user_id != row.owner_user_id or row.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "branch artifact is unavailable")
    return row


@router.get("/{artifact_id}/metadata", response_model=BranchArtifact)
async def artifact_metadata(
    artifact_id: uuid.UUID,
    principal: CurrentAuthenticated,
    session: SessionDep,
) -> BranchArtifact:
    row = await _owned_artifact(artifact_id, principal, session)
    return BranchArtifact(
        id=row.id,
        owner_user_id=row.owner_user_id,
        run_id=row.run_id,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        expires_at=row.expires_at,
        collected_at=row.collected_at,
    )


@router.get("/{artifact_id}")
async def download_artifact(
    artifact_id: uuid.UUID,
    principal: CurrentAuthenticated,
    session: SessionDep,
    request: Request,
) -> StreamingResponse:
    row = await _owned_artifact(artifact_id, principal, session)
    node_name, run_id, size, digest = row.storage_ref, row.run_id, row.size_bytes, row.sha256
    try:
        async with NodeClient(request.app.state.settings) as client:
            status_now = await client.workspace_artifact_status(node_name, run_id)
    except NodeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Studio branch artifact is unavailable"
        ) from exc
    if (
        status_now.artifact_id != artifact_id
        or status_now.size_bytes != size
        or status_now.sha256 != digest
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "branch artifact changed after collection")

    async def chunks() -> AsyncIterator[bytes]:
        with tracer.start_as_current_span("coire.api.mcp.artifact.download") as span:
            span.set_attribute("run_id", str(run_id))
            span.set_attribute("user_id", str(principal.user_id))
            span.set_attribute("artifact_id", str(artifact_id))
            try:
                async with NodeClient(request.app.state.settings, timeout=120) as client:
                    async for chunk in client.stream_workspace_artifact(
                        node_name, run_id, expected_size=size, expected_sha256=digest
                    ):
                        yield chunk
            except Exception:
                downloads_total.add(1, {"outcome": "failed"})
                logger.exception(
                    "MCP artifact download failed run_id=%s artifact_id=%s user_id=%s",
                    run_id,
                    artifact_id,
                    principal.user_id,
                )
                raise
            downloads_total.add(1, {"outcome": "succeeded"})
            logger.info(
                "MCP artifact downloaded run_id=%s artifact_id=%s user_id=%s",
                run_id,
                artifact_id,
                principal.user_id,
            )

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact_id}.bundle"',
            "Content-Length": str(size),
            "X-Coire-Sha256": digest,
            "Cache-Control": "private, no-store",
        },
    )
