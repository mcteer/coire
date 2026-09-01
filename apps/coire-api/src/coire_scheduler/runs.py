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
    ModelCopyRow,
    NodeRow,
    RunCommandRow,
    session_scope,
)
from coire_api.run_tokens import revoke_run_token
from coire_api.runs import run_command_id, transition
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
        run = await session.get(AgentRunRow, run_id)
        if run is not None and run.state is not state and run.state not in TERMINAL_RUN_STATES:
            await transition(session, run, state, reason)
            transitions_total.add(1, {"state": state.value})
            last_transition.set(time.time(), {"state": state.value})
            logger.info("run transition run_id=%s state=%s", run_id, state.value)


async def _submit(run_id: uuid.UUID, operation: RunOperation) -> dict[str, Any]:
    identifier = run_command_id(run_id, operation)
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id)
        if run is None or run.node_id is None:
            raise RuntimeError("run has no Studio placement")
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
        await asyncio.sleep(get_settings().placement_poll_interval_s)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def place_run(run_id_text: str) -> None:
    run_id = uuid.UUID(run_id_text)
    await _advance(run_id, AgentRunState.PLACING, "Studio placement started")
    while True:
        node_id = await choose_studio(run_id)
        if node_id is not None:
            async with session_scope() as session:
                run = await session.get(AgentRunRow, run_id, with_for_update=True)
                if run is not None and run.node_id is None:
                    run.node_id = node_id
                    run.updated_at = datetime.now(UTC)
            return
        queued_total.add(1)
        await asyncio.sleep(get_settings().placement_poll_interval_s)


@DBOS.step(retries_allowed=True, max_attempts=5, interval_seconds=1.0)
async def execute_run(run_id_text: str) -> None:
    run_id = uuid.UUID(run_id_text)
    with tracer.start_as_current_span("coire.scheduler.run.execute") as span:
        span.set_attribute("run_id", run_id_text)
        await _advance(run_id, AgentRunState.CREATING, "creating hardened container")
        created = await _submit(run_id, RunOperation.CREATE)
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            if run is not None:
                run.container_id = str(created["container_id"])
        await _submit(run_id, RunOperation.START)
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
            raise TimeoutError("run_timeout")
        if waited.get("exit_code") not in (None, 0):
            raise RuntimeError("run_container_failed")
        await _advance(run_id, AgentRunState.COLLECTING, "collecting strict result")
        collected = await _submit(run_id, RunOperation.COLLECT)
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
            if run is not None:
                result = collected.get("result")
                run.result = result if isinstance(result, dict) else None


@DBOS.step(retries_allowed=True, max_attempts=5, interval_seconds=1.0)
async def finalize_run(run_id_text: str, succeeded: bool, detail: str = "") -> None:
    run_id = uuid.UUID(run_id_text)
    async with session_scope() as session:
        run = await session.get(AgentRunRow, run_id)
        if run is None:
            return
        await revoke_run_token(session, run_id)
        if run.state not in TERMINAL_RUN_STATES:
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
    try:
        await _submit(run_id, RunOperation.REMOVE)
    except Exception:
        logger.exception("run cleanup pending run_id=%s", run_id)


@DBOS.workflow(name="coire.run.workflow", max_recovery_attempts=100)
async def run_workflow(run_id_text: str) -> None:
    try:
        await place_run(run_id_text)
        await execute_run(run_id_text)
    except Exception as exc:
        await finalize_run(run_id_text, False, str(exc) or type(exc).__name__.lower())
        return
    await finalize_run(run_id_text, True)


@DBOS.workflow(name="coire.run.kill", max_recovery_attempts=100)
async def run_kill_workflow(run_id_text: str) -> None:
    run_id = uuid.UUID(run_id_text)
    try:
        await _submit(run_id, RunOperation.KILL)
    finally:
        async with session_scope() as session:
            run = await session.get(AgentRunRow, run_id)
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
