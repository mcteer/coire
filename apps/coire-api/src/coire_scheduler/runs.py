"""DBOS-owned durable lifecycle and Studio-only placement for agent runs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api.audit import write_audit
from coire_api.db import (
    AgentRunRow,
    McpArtifactRow,
    McpCallRow,
    ModelCopyRow,
    NodeRow,
    RunCommandRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.run_tokens import revoke_run_token
from coire_api.runs import run_command_id, transition
from coire_core.models.mcp import ApplyResult, McpToolName
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRunState,
    RunCommandState,
    RunOperation,
    RunResourceUsage,
)
from coire_core.settings import get_settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.runs")
meter = metrics.get_meter("coire.scheduler.runs")
transitions_total = meter.create_counter("coire_run_transitions_total", unit="1")
queued_total = meter.create_counter("coire_run_capacity_waits_total", unit="1")
last_transition = meter.create_gauge("coire_run_last_transition_timestamp_seconds", unit="s")


def rank_studio_candidates(
    nodes: list[NodeRow],
    counts: dict[uuid.UUID, int],
    model_copy_nodes: set[uuid.UUID],
    *,
    cap: int,
) -> list[NodeRow]:
    candidates = [
        node
        for node in nodes
        if node.role is NodeRole.STUDIO
        and node.reachability is Reachability.HEALTHY
        and counts.get(node.id, 0) < cap
    ]
    candidates.sort(
        key=lambda node: (
            node.id not in model_copy_nodes,
            counts.get(node.id, 0),
            node.name,
        )
    )
    return candidates


async def choose_studio(run_id: uuid.UUID) -> uuid.UUID | None:
    """Choose only a healthy Studio, preferring a verified local primary-model copy."""
    settings = get_settings()
    if settings.placement_sandbox_bytes <= 0:
        return None
    freshness = datetime.now(UTC) - timedelta(seconds=settings.placement_health_freshness_s)
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id)
        if run is None:
            return None
        fifo_head = await session.scalar(
            select(AgentRunRow.id)
            .where(
                AgentRunRow.node_id.is_(None),
                AgentRunRow.state.in_([AgentRunState.QUEUED, AgentRunState.PLACING]),
            )
            .order_by(AgentRunRow.requested_at, AgentRunRow.id)
            .limit(1)
        )
        if fifo_head != run_id:
            return None
        nodes = list(
            (
                await session.scalars(
                    select(NodeRow).where(
                        NodeRow.role == NodeRole.STUDIO,
                        NodeRow.reachability == Reachability.HEALTHY,
                        NodeRow.last_seen_at >= freshness,
                        NodeRow.control_host.is_not(None),
                    )
                )
            ).all()
        )
        active = list(
            (
                await session.scalars(
                    select(AgentRunRow).where(
                        AgentRunRow.node_id.in_([node.id for node in nodes]),
                        AgentRunRow.state.notin_(list(TERMINAL_RUN_STATES)),
                        AgentRunRow.id != run_id,
                    )
                )
            ).all()
        )
        counts = {node.id: 0 for node in nodes}
        for item in active:
            if item.node_id in counts:
                counts[item.node_id] += 1
        copies = set(
            (
                await session.scalars(
                    select(ModelCopyRow.node_id).where(
                        ModelCopyRow.model_id == run.primary_model_id,
                        ModelCopyRow.verified.is_(True),
                    )
                )
            ).all()
        )
        candidates = rank_studio_candidates(nodes, counts, copies, cap=settings.run_concurrency_cap)
        return candidates[0].id if candidates else None


async def _advance(run_id: uuid.UUID, state: AgentRunState, reason: str) -> None:
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id, with_for_update=True)
        if run is not None and run.state is not state and run.state not in TERMINAL_RUN_STATES:
            if run.state is AgentRunState.KILL_REQUESTED:
                return
            await transition(session, run, state, reason)
            transitions_total.add(1, {"state": state.value})
            last_transition.set(time.time(), {"state": state.value})
            logger.info("run transition run_id=%s state=%s", run_id, state.value)


async def _submit(run_id: uuid.UUID, operation: RunOperation) -> dict[str, Any]:
    identifier = run_command_id(run_id, operation)
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id, with_for_update=True)
        if run is None or run.node_id is None:
            raise RuntimeError("run has no Studio placement")
        if run.state is AgentRunState.KILL_REQUESTED and operation not in {
            RunOperation.KILL,
            RunOperation.REMOVE,
        }:
            raise RuntimeError("run kill requested")
        row = await session.get(RunCommandRow, identifier)
        if row is None:
            session.add(
                RunCommandRow(
                    id=identifier,
                    run_id=run_id,
                    node_id=run.node_id,
                    operation=operation,
                    attempt=1,
                    state=RunCommandState.PENDING,
                    detail={},
                )
            )
    while True:
        async with session_scope() as session:
            row = await session.get(RunCommandRow, identifier)
            if row is None:
                raise RuntimeError("run command disappeared")
            if row.state is RunCommandState.SUCCEEDED:
                return dict(row.detail)
            if row.state is RunCommandState.FAILED:
                raise RuntimeError(str(row.detail.get("failure_code", "run_command_failed")))
        settings = get_settings()
        await asyncio.sleep(
            settings.run_kill_poll_interval_s
            if operation is RunOperation.KILL
            else settings.placement_poll_interval_s
        )


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def place_run(run_id_text: str) -> None:
    run_id = uuid.UUID(run_id_text)
    await _advance(run_id, AgentRunState.PLACING, "Studio placement started")
    while True:
        async with session_scope() as session:
            pending = await session.get(AgentRunRow, run_id)
            if pending is None or pending.state in {
                AgentRunState.KILL_REQUESTED,
                AgentRunState.KILLED,
            }:
                return
        node_id = await choose_studio(run_id)
        if node_id is not None:
            async with session_scope() as session:
                run = await session.get(AgentRunRow, run_id, with_for_update=True)
                if (
                    run is not None
                    and run.node_id is None
                    and run.state is not AgentRunState.KILL_REQUESTED
                ):
                    run.node_id = node_id
                    run.updated_at = datetime.now(UTC)
            return
        queued_total.add(1)
        await asyncio.sleep(get_settings().placement_poll_interval_s)


# A process restart interrupts the active WAIT command and DBOS records the recovery as another
# step attempt. Keep the ceiling aligned with the workflow recovery budget so several supervisor
# restarts cannot exhaust otherwise idempotent command replay while a container is still running.
@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def execute_run(run_id_text: str) -> str | None:
    run_id = uuid.UUID(run_id_text)
    with tracer.start_as_current_span("coire.scheduler.run.execute") as span:
        span.set_attribute("run_id", run_id_text)
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            container_id = run.container_id if run is not None else None
            current_state = run.state if run is not None else None
        if container_id is None:
            await _advance(run_id, AgentRunState.CREATING, "creating hardened container")
            created = await _submit(run_id, RunOperation.CREATE)
            async with session_scope() as session:
                run = await session.get(AgentRunRow, run_id)
                if run is not None:
                    run.container_id = str(created["container_id"])
            current_state = AgentRunState.CREATING
        await _submit(run_id, RunOperation.START)
        if current_state in {
            AgentRunState.QUEUED,
            AgentRunState.PLACING,
            AgentRunState.CREATING,
        }:
            await _advance(run_id, AgentRunState.RUNNING, "container started")
        waited = await _submit(run_id, RunOperation.WAIT)
        logs = await _submit(run_id, RunOperation.LOGS)
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            if run is not None:
                run.exit_code = (
                    int(waited["exit_code"]) if waited.get("exit_code") is not None else None
                )
                items = logs.get("items", [])
                log_bytes = (
                    sum(
                        len(str(item.get("content", "")).encode())
                        for item in items
                        if isinstance(item, dict)
                    )
                    if isinstance(items, list)
                    else 0
                )
                usage = RunResourceUsage.model_validate(waited.get("resource_usage") or {})
                usage.log_bytes = log_bytes
                run.resource_usage = usage.model_dump(mode="json")
        if waited.get("state") == "timed_out":
            # A container timeout is an expected terminal outcome, not a transient DBOS
            # step failure. Returning the detail lets the workflow finalize immediately
            # instead of retrying this step up to its recovery ceiling.
            return "run_timeout"
        if waited.get("exit_code") not in (None, 0):
            # Likewise, non-zero exits (including an external/OOM kill such as 137) must
            # become a terminal run state rather than replaying the whole execution step.
            return "run_container_failed"
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            current_state = run.state if run is not None else None
        if current_state is not AgentRunState.COLLECTING:
            await _advance(run_id, AgentRunState.COLLECTING, "collecting strict result")
        collected = await _submit(run_id, RunOperation.COLLECT)
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            if run is not None:
                result = collected.get("result")
                run.result = result if isinstance(result, dict) else None
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            call = (
                await session.get(McpCallRow, run.prepared_request_id)
                if run is not None and run.prepared_request_id is not None
                else None
            )
            node = (
                await session.get(NodeRow, run.node_id) if run is not None and run.node_id else None
            )
            output = run.result.get("output") if run is not None and run.result else None
            if call is not None and call.tool is McpToolName.APPLY:
                if node is None or not isinstance(output, dict):
                    raise RuntimeError("MCP apply result or node is unavailable")
                apply = ApplyResult.model_validate(output)
                settings = get_settings()
                async with NodeClient(settings) as client:
                    artifact = await client.workspace_artifact_status(node.name, run_id)
                if artifact.artifact_id != apply.artifact_id or artifact.run_id != run_id:
                    raise RuntimeError("MCP branch artifact identity mismatch")
                if await session.get(McpArtifactRow, artifact.artifact_id) is None:
                    session.add(
                        McpArtifactRow(
                            id=artifact.artifact_id,
                            call_id=call.id,
                            owner_user_id=call.owner_user_id,
                            run_id=run_id,
                            sha256=artifact.sha256,
                            size_bytes=artifact.size_bytes,
                            storage_ref=node.name,
                            collected_at=artifact.collected_at,
                            expires_at=artifact.collected_at
                            + timedelta(hours=settings.mcp_artifact_retention_hours),
                        )
                    )
    return None


@DBOS.step(retries_allowed=True, max_attempts=5, interval_seconds=1.0)
async def finalize_run(run_id_text: str, succeeded: bool, detail: str = "") -> None:
    run_id = uuid.UUID(run_id_text)
    async with session_scope() as session:
        staged = await session.get(AgentRunRow, run_id)
        node = (
            await session.get(NodeRow, staged.node_id)
            if staged is not None and staged.node_id is not None
            else None
        )
        node_name = node.name if node is not None else None
    if node_name is not None:
        from coire_api.chat.activity import collect_run_activity

        try:
            with tracer.start_as_current_span(
                "coire.scheduler.chat.activity",
                attributes={"run_id": str(run_id)},
                record_exception=False,
                set_status_on_exception=False,
            ):
                await collect_run_activity(run_id, node_name, get_settings(), final=True)
        except NodeError as exc:
            if exc.retryable:
                raise
            logger.error(
                "chat final activity unavailable run_id=%s error_type=%s",
                run_id,
                type(exc).__name__,
            )
        except ValueError as exc:
            logger.error(
                "chat final activity invalid run_id=%s error_type=%s", run_id, type(exc).__name__
            )
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id, with_for_update=True)
        if run is None:
            return
        if run.state not in TERMINAL_RUN_STATES and run.state is not AgentRunState.KILL_REQUESTED:
            await revoke_run_token(session, run_id)
            state = AgentRunState.SUCCEEDED
            if not succeeded:
                state = (
                    AgentRunState.TIMED_OUT
                    if "run_timeout" in detail
                    else AgentRunState.RESULT_COLLECTION_FAILED
                    if "run_result_" in detail
                    else AgentRunState.FAILED
                )
            if not succeeded:
                run.failure_code = detail[:64] or "run_failed"
                run.failure_detail = "run failed; inspect correlated logs"
            await transition(session, run, state, "run completed" if succeeded else "run failed")
            await write_audit(
                session,
                actor="coire-scheduler",
                action=f"agent_run.{state.value}",
                target_type="agent_run",
                target_id=run_id_text,
                detail={"node_id": str(run.node_id), "exit_code": run.exit_code},
            )
            transitions_total.add(1, {"state": state.value})
            last_transition.set(time.time(), {"state": state.value})
    from coire_api.chat.coding import reconcile_chat_coding_result

    await reconcile_chat_coding_result(run_id, get_settings())
    try:
        await _submit(run_id, RunOperation.REMOVE)
    except Exception:
        logger.exception("run cleanup pending run_id=%s", run_id)


@DBOS.workflow(name="coire.run.workflow", max_recovery_attempts=100)
async def run_workflow(run_id_text: str) -> None:
    try:
        await place_run(run_id_text)
        async with session_scope() as session:
            placed = await session.get(AgentRunRow, uuid.UUID(run_id_text))
            if placed is None or placed.state in {
                AgentRunState.KILL_REQUESTED,
                AgentRunState.KILLED,
            }:
                return
        detail = await execute_run(run_id_text)
    except Exception as exc:
        await finalize_run(run_id_text, False, str(exc) or type(exc).__name__.lower())
        return
    if detail is not None:
        await finalize_run(run_id_text, False, detail)
        return
    await finalize_run(run_id_text, True)


@DBOS.workflow(name="coire.run.kill", max_recovery_attempts=100)
async def run_kill_workflow(run_id_text: str) -> None:
    run_id = uuid.UUID(run_id_text)
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id)
        if run is None or run.state is not AgentRunState.KILL_REQUESTED:
            return
        placed = run.node_id is not None
        node = await session.get(NodeRow, run.node_id) if run.node_id is not None else None
    if placed:
        if node is not None:
            from coire_api.chat.activity import collect_run_activity

            try:
                with tracer.start_as_current_span(
                    "coire.scheduler.chat.activity",
                    attributes={"run_id": str(run_id)},
                    record_exception=False,
                    set_status_on_exception=False,
                ):
                    await collect_run_activity(run_id, node.name, get_settings(), final=True)
            except (NodeError, ValueError) as exc:
                logger.error(
                    "chat kill activity unavailable run_id=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
        await _submit(run_id, RunOperation.KILL)
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id, with_for_update=True)
        if run is not None and run.state is AgentRunState.KILL_REQUESTED:
            await transition(session, run, AgentRunState.KILLED, "container kill completed")
            await write_audit(
                session,
                actor="coire-scheduler",
                action="agent_run.killed",
                target_type="agent_run",
                target_id=run_id_text,
                detail={"node_id": str(run.node_id)},
            )
    from coire_api.chat.coding import reconcile_chat_coding_result

    await reconcile_chat_coding_result(run_id, get_settings())
