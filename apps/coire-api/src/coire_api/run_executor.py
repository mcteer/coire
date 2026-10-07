"""Execute durable run commands through the authenticated Studio node boundary."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    AgentRunRow,
    EntitlementRow,
    McpCallRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    RunCommandRow,
    TrainingAdapterRow,
    UserRow,
    session_scope,
)
from coire_api.evaluations import target_is_write_verified
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.polling import PollBackoff, wait_or_stop
from coire_api.run_tokens import rotate_run_token
from coire_api.runs import authorize_run_target, run_target
from coire_api.workspaces import resolve_source
from coire_core.models.auth import UserRole
from coire_core.models.conversation import ImagePart
from coire_core.models.harness import HarnessRunRequest, ProfileName, TaskClass
from coire_core.models.mcp import WorkspaceSource
from coire_core.models.node import WorkspacePrepareRequest, WorkspaceVisualInput
from coire_core.models.registry import CapabilityProfile
from coire_core.models.runs import (
    AgentRunState,
    RunCommandState,
    RunContainerCreate,
    RunLimits,
    RunOperation,
    RunTokenScope,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.runs")
meter = metrics.get_meter("coire.api.runs")
commands_total = meter.create_counter("coire_run_commands_total", unit="1")
kill_latency = meter.create_histogram("coire_run_kill_queue_latency_seconds", unit="s")


def _coding_visual_inputs(call: McpCallRow) -> list[WorkspaceVisualInput]:
    raw = call.input.get("coire_visual_inputs", []) if isinstance(call.input, dict) else []
    if not isinstance(raw, list):
        raise RuntimeError("MCP visual control inputs are invalid")
    return [WorkspaceVisualInput.model_validate(value) for value in raw]


class RunCommandExecutor:
    def __init__(self, settings: Settings, *, external_kill_scan: bool = False) -> None:
        self.settings = settings
        self.external_kill_scan = external_kill_scan
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._kill_task: asyncio.Task[None] | None = None
        self._kills: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._kill_nodes: dict[uuid.UUID, uuid.UUID] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="run-command-executor")
            if not self.external_kill_scan:
                self._kill_task = asyncio.create_task(self._run_kills(), name="run-kill-executor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    self._task, timeout=self.settings.scheduler_shutdown_timeout_s
                )
            except TimeoutError:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        if self._kill_task is not None:
            await self._kill_task
            self._kill_task = None
        if self._kills:
            for task in self._kills.values():
                task.cancel()
            await asyncio.gather(*self._kills.values(), return_exceptions=True)
            self._kills.clear()
            self._kill_nodes.clear()

    async def _run_kills(self) -> None:
        """Reserve a short independent lane so WAIT cannot delay a kill."""
        while not self._stop.is_set():
            try:
                async with session_scope() as session:
                    rows = list(
                        (
                            await session.execute(
                                select(RunCommandRow.id, RunCommandRow.node_id)
                                .where(
                                    RunCommandRow.operation == RunOperation.KILL,
                                    RunCommandRow.state.in_(
                                        [RunCommandState.PENDING, RunCommandState.RUNNING]
                                    ),
                                )
                                .order_by(RunCommandRow.created_at)
                            )
                        ).tuples()
                    )
                self.enqueue_kills(rows)
            except Exception:
                logger.exception("run kill queue poll failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.run_kill_poll_interval_s
                )

    def enqueue_kills(self, rows: list[tuple[uuid.UUID, uuid.UUID | None]]) -> None:
        active_by_node: dict[uuid.UUID, int] = {}
        for active_node_id in self._kill_nodes.values():
            active_by_node[active_node_id] = active_by_node.get(active_node_id, 0) + 1
        for command_id, node_id in rows:
            if node_id is None or command_id in self._kills:
                continue
            if active_by_node.get(node_id, 0) >= self.settings.run_concurrency_cap:
                continue
            active_by_node[node_id] = active_by_node.get(node_id, 0) + 1
            task = asyncio.create_task(self._execute_safely(command_id))
            self._kills[command_id] = task
            self._kill_nodes[command_id] = node_id
            task.add_done_callback(partial(self._finished_kill, command_id))

    def _finished_kill(self, command_id: uuid.UUID, _task: asyncio.Task[None]) -> None:
        self._kills.pop(command_id, None)
        self._kill_nodes.pop(command_id, None)

    async def _run(self) -> None:
        backoff = PollBackoff(
            self.settings.placement_poll_interval_s,
            self.settings.scheduler_idle_scan_max_s,
            self.settings.scheduler_failure_backoff_max_s,
        )
        while not self._stop.is_set():
            try:
                command_id = await self._next()
                if command_id is not None:
                    backoff.active()
                    await self._execute_safely(command_id)
                    continue
            except Exception:
                logger.exception("run command queue poll failed")
                await wait_or_stop(self._stop, backoff.failed())
                continue
            await wait_or_stop(self._stop, backoff.idle())

    async def _next(self) -> uuid.UUID | None:
        async with session_scope() as session:
            command_id: uuid.UUID | None = await session.scalar(
                select(RunCommandRow.id)
                .where(
                    RunCommandRow.operation != RunOperation.KILL,
                    RunCommandRow.state.in_([RunCommandState.PENDING, RunCommandState.RUNNING]),
                )
                .order_by(RunCommandRow.created_at)
                .limit(1)
            )
            return command_id

    async def _execute_safely(self, command_id: uuid.UUID) -> None:
        try:
            result = await self._execute(command_id)
        except NodeError as exc:
            if exc.retryable:
                await asyncio.sleep(self.settings.placement_poll_interval_s)
                return
            await self._failed(command_id, exc)
            return
        except Exception as exc:
            await self._failed(command_id, exc)
            return
        async with session_scope() as session:
            row = await session.get(RunCommandRow, command_id)
            if row is not None:
                row.state = RunCommandState.SUCCEEDED
                row.detail = result
                row.updated_at = datetime.now(UTC)
                commands_total.add(1, {"operation": row.operation.value, "outcome": "succeeded"})
                if row.operation is RunOperation.KILL:
                    kill_latency.record(
                        max(0.0, (datetime.now(UTC) - row.created_at).total_seconds()),
                        {"outcome": "succeeded"},
                    )

    async def _failed(self, command_id: uuid.UUID, exc: Exception) -> None:
        logger.exception("run command failed command_id=%s", command_id, exc_info=exc)
        async with session_scope() as session:
            row = await session.get(RunCommandRow, command_id)
            if row is not None:
                node_detail = exc.body.get("detail") if isinstance(exc, NodeError) else None
                node_code = node_detail.get("code") if isinstance(node_detail, dict) else None
                row.state = RunCommandState.FAILED
                row.detail = {
                    "failure_code": str(node_code or type(exc).__name__.lower())[:64],
                    "failure_detail": "node run operation failed; inspect correlated logs",
                }
                row.updated_at = datetime.now(UTC)
                commands_total.add(1, {"operation": row.operation.value, "outcome": "failed"})
                if row.operation is RunOperation.KILL:
                    kill_latency.record(
                        max(0.0, (datetime.now(UTC) - row.created_at).total_seconds()),
                        {"outcome": "failed"},
                    )

    async def _poll_chat_activity(self, run_id: uuid.UUID, node_name: str) -> None:
        """Persist live receipts while WAIT owns the run; finalization drains the tail."""
        from coire_api.chat.activity import activity_cursor, collect_run_activity

        if await activity_cursor(run_id) is None:
            return
        while True:
            try:
                await collect_run_activity(run_id, node_name, self.settings)
            except Exception as exc:
                logger.error(
                    "chat live activity poll failed run_id=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
            await asyncio.sleep(0.5)

    async def _execute(self, command_id: uuid.UUID) -> dict[str, object]:
        with tracer.start_as_current_span("coire.api.run.command") as span:
            span.set_attribute("command_id", str(command_id))
            async with session_scope() as session:
                command = await session.get(RunCommandRow, command_id)
                if command is None:
                    raise RuntimeError("run command disappeared")
                run = await session.get(AgentRunRow, command.run_id)
                node = await session.get(NodeRow, command.node_id) if command.node_id else None
                if run is None or node is None:
                    raise RuntimeError("run command references missing state")
                if run.state is AgentRunState.KILL_REQUESTED and command.operation not in {
                    RunOperation.KILL,
                    RunOperation.REMOVE,
                }:
                    raise RuntimeError("run kill requested")
                command.state = RunCommandState.RUNNING
                command.updated_at = datetime.now(UTC)
                operation, node_name, run_id = command.operation, node.name, run.id
                run_limits = RunLimits.model_validate(run.limits)
                span.set_attribute("run_id", str(run_id))
                span.set_attribute("node", node_name)
                target = run_target(run)
                if operation is RunOperation.START:
                    await self._authorize_execution(session, run)

            # The wait route is intentionally a blocking node operation. Its HTTP
            # budget must cover the admitted run timeout; otherwise a healthy long
            # run is retried forever in the command ledger after five seconds.
            node_timeout = 5.0
            if operation is RunOperation.CREATE:
                node_timeout = max(
                    self.settings.run_relay_start_timeout_s + 10.0,
                    self.settings.mcp_workspace_prepare_timeout_s + 10.0,
                )
            elif operation is RunOperation.WAIT:
                node_timeout = float(run_limits.timeout_seconds + 5)
            async with NodeClient(self.settings, timeout=node_timeout) as client:
                if operation is RunOperation.CREATE:
                    observations = await client.list_runs(node_name)
                    existing = next((item for item in observations if item.run_id == run_id), None)
                    if existing is not None and run.container_id == existing.container_id:
                        return {
                            "run_id": str(run_id),
                            "container_id": existing.container_id,
                            "state": existing.state,
                            "hardened": True,
                        }
                    async with session_scope() as session:
                        await self._authorize_execution(session, run)
                    if not self.settings.run_agent_image:
                        raise RuntimeError("COIRE_RUN_AGENT_IMAGE must be digest-pinned")
                    prepared = None
                    if run.prepared_request_id is not None:
                        async with session_scope() as session:
                            mcp_call = await session.get(McpCallRow, run.prepared_request_id)
                            model = await session.get(ModelRow, run.primary_model_id)
                            if (
                                mcp_call is None
                                or mcp_call.run_id != run_id
                                or mcp_call.owner_user_id != run.requester_user_id
                                or model is None
                            ):
                                raise RuntimeError("MCP run preparation identity is invalid")
                            visual_inputs = _coding_visual_inputs(mcp_call)
                            prepare = WorkspacePrepareRequest(
                                run_id=run_id,
                                source=await resolve_source(
                                    session,
                                    owner_user_id=run.requester_user_id,
                                    source=WorkspaceSource.model_validate(mcp_call.source),
                                    settings=self.settings,
                                ),
                                task_class=run.task_class,
                                harness_request=HarnessRunRequest(
                                    profile=ProfileName(run.profile),
                                    variant_id=run.primary_variant_id,
                                    target=target,
                                    task_class=run.task_class,
                                    coding_mode=mcp_call.tool,
                                    coding_call_id=mcp_call.id,
                                    task=mcp_call.task,
                                    visual_inputs=[
                                        ImagePart(
                                            asset_id=image.asset_id,
                                            media_type="image/png",
                                            width=image.width,
                                            height=image.height,
                                        )
                                        for image in visual_inputs
                                    ],
                                    capability_profile=CapabilityProfile.model_validate(
                                        model.capability_profile or {}
                                    ),
                                    context_window=model.context_window or 4096,
                                ),
                                visual_inputs=visual_inputs,
                            )
                        prepared = await client.prepare_workspace(node_name, prepare)
                        if prepared.run_id != run_id:
                            raise RuntimeError("Studio returned another run workspace")
                    async with session_scope() as session:
                        locked = await session.get(AgentRunRow, run_id, with_for_update=True)
                        if locked is None:
                            raise RuntimeError("run disappeared before token mint")
                        if locked.state is AgentRunState.KILL_REQUESTED:
                            raise RuntimeError("run kill requested before container creation")
                        variant = await session.get(ModelVariantRow, locked.primary_variant_id)
                        await self._authorize_execution(session, locked)
                        if (
                            variant is None
                            or not variant.validated
                            or not variant.published
                            or (
                                locked.task_class is TaskClass.WRITE
                                and not (
                                    await target_is_write_verified(session, target)
                                    if target
                                    else variant.harness_verified
                                )
                            )
                        ):
                            raise RuntimeError("run variant is no longer eligible")
                        if prepared is not None:
                            locked.workspace_ref = prepared.workspace_ref
                            locked.output_ref = prepared.output_ref
                        scope = RunTokenScope.model_validate(locked.token_scope)
                        limits = RunLimits.model_validate(locked.limits)
                        _, plaintext = await rotate_run_token(
                            session,
                            locked,
                            scope,
                            ttl_seconds=min(
                                86_400,
                                max(self.settings.run_token_ttl_s, limits.timeout_seconds),
                            ),
                        )
                        selector: uuid.UUID | str = locked.primary_model_id
                        if target and target.adapter_id:
                            adapter = await session.get(TrainingAdapterRow, target.adapter_id)
                            if adapter is None:
                                raise RuntimeError("run adapter disappeared")
                            selector = adapter.selector
                        create = RunContainerCreate(
                            run_id=run_id,
                            profile=ProfileName(locked.profile),
                            model_id=locked.primary_model_id,
                            variant_id=locked.primary_variant_id,
                            target=target,
                            public_selector=selector,
                            harness_verified=await target_is_write_verified(session, target)
                            if target
                            else variant.harness_verified,
                            image=self.settings.run_agent_image,
                            argv=["-m", "coire_agent"],
                            workspace_ref=locked.workspace_ref,
                            task_class=locked.task_class,
                            output_ref=locked.output_ref,
                            run_token=plaintext,
                            gateway_url=self.settings.run_gateway_url,
                            limits=limits,
                        )
                    return (await client.create_run(node_name, create)).model_dump(mode="json")
                if operation is RunOperation.START:
                    return (await client.start_run(node_name, run_id)).model_dump(mode="json")
                if operation is RunOperation.LOGS:
                    chunks = await client.run_logs(node_name, run_id)
                    return {"items": [item.model_dump(mode="json") for item in chunks]}
                if operation is RunOperation.WAIT:
                    polling = asyncio.create_task(
                        self._poll_chat_activity(run_id, node_name),
                        name=f"chat-activity-{run_id}",
                    )
                    try:
                        return (await client.wait_run(node_name, run_id)).model_dump(mode="json")
                    finally:
                        polling.cancel()
                        with suppress(asyncio.CancelledError):
                            await polling
                if operation is RunOperation.COLLECT:
                    return (await client.collect_run(node_name, run_id)).model_dump(mode="json")
                if operation in {RunOperation.REMOVE, RunOperation.KILL}:
                    call = client.remove_run(node_name, run_id, kill=operation is RunOperation.KILL)
                    if operation is RunOperation.KILL:
                        await asyncio.wait_for(call, timeout=4.0)
                    else:
                        await call
                    return {"removed": True}
            raise RuntimeError(f"unsupported run operation {operation.value}")

    async def _authorize_execution(self, session: AsyncSession, run: AgentRunRow) -> None:
        user = await session.get(UserRow, run.requester_user_id)
        model = await session.get(ModelRow, run.primary_model_id)
        entitlements = set(
            (
                await session.scalars(
                    select(EntitlementRow.name).where(
                        EntitlementRow.user_id == run.requester_user_id,
                        EntitlementRow.revoked_at.is_(None),
                    )
                )
            ).all()
        )
        is_admin = user is not None and user.role is UserRole.ADMIN
        if (
            user is None
            or not user.active
            or model is None
            or model.state.value != "ready"
            or (
                not is_admin
                and (
                    model.visibility.value != "published"
                    or not set(model.entitlement).issubset(entitlements)
                )
            )
        ):
            raise RuntimeError("run requester no longer authorized")
        target = run_target(run)
        if target is not None:
            await authorize_run_target(session, target, run.task_class, is_admin, entitlements)
        elif run.primary_adapter_id is not None:
            raise RuntimeError("adapter run is missing exact grants")
        else:
            variant = await session.get(ModelVariantRow, run.primary_variant_id)
            if (
                variant is None
                or not variant.validated
                or not variant.published
                or (run.task_class is TaskClass.WRITE and not variant.harness_verified)
            ):
                raise RuntimeError("run variant is no longer eligible")
