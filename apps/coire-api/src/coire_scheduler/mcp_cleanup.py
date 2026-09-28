"""Bounded Studio workspace and retained MCP result expiry."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api.db import AgentRunRow, McpArtifactRow, McpCallRow, NodeRow, session_scope
from coire_api.nodes_client import NodeClient
from coire_core.models.node import WorkspaceCleanupRequest
from coire_core.models.runs import TERMINAL_RUN_STATES
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.mcp")
cleanup_total = metrics.get_meter("coire.scheduler.mcp").create_counter(
    "coire_mcp_workspace_cleanup_total", unit="1"
)


async def sweep_mcp_workspaces(settings: Settings) -> None:
    now = datetime.now(UTC)
    with tracer.start_as_current_span("coire.scheduler.mcp.cleanup"):
        async with session_scope() as session:
            call_ids = list(
                (
                    await session.scalars(
                        select(McpCallRow.id)
                        .where(
                            McpCallRow.workspace_cleaned_at.is_(None),
                            McpCallRow.run_id.is_not(None),
                        )
                        .order_by(McpCallRow.requested_at)
                        .limit(100)
                    )
                ).all()
            )
        for call_id in call_ids:
            try:
                async with session_scope() as session:
                    call = await session.get(McpCallRow, call_id)
                    if call is None or call.run_id is None or call.workspace_cleaned_at is not None:
                        continue
                    run = await session.get(AgentRunRow, call.run_id)
                    if (
                        run is None
                        or run.state not in TERMINAL_RUN_STATES
                        or run.finished_at is None
                    ):
                        continue
                    artifact = await session.scalar(
                        select(McpArtifactRow).where(McpArtifactRow.call_id == call.id)
                    )
                    expiry = (
                        artifact.expires_at + timedelta(minutes=5)
                        if artifact is not None
                        else run.finished_at + timedelta(hours=24)
                        if run.state.value == "result_collection_failed"
                        else run.finished_at + timedelta(hours=1)
                    )
                    if expiry > now:
                        continue
                    if run.node_id is None:
                        call.workspace_cleaned_at = now
                        continue
                    node = await session.get(NodeRow, run.node_id)
                    if node is None:
                        continue
                    async with NodeClient(settings) as client:
                        await client.cleanup_workspace(
                            node.name, WorkspaceCleanupRequest(run_id=run.id)
                        )
                    call.workspace_cleaned_at = now
                    if artifact is not None:
                        await session.delete(artifact)
                    cleanup_total.add(1, {"tool": call.tool.value, "outcome": "succeeded"})
            except Exception:
                cleanup_total.add(1, {"outcome": "failed"})
                logger.exception("MCP workspace cleanup failed call_id=%s", call_id)
        async with session_scope() as session:
            expired = list(
                (
                    await session.scalars(
                        select(McpCallRow)
                        .where(
                            McpCallRow.workspace_cleaned_at.is_not(None),
                            McpCallRow.finished_at
                            < now - timedelta(hours=settings.mcp_artifact_retention_hours),
                        )
                        .limit(100)
                    )
                ).all()
            )
            for call in expired:
                await session.delete(call)
