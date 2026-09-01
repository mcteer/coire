"""Execute durable sharding commands from the credential-bearing API process."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast

from opentelemetry import trace
from sqlalchemy import select

from coire_api.db import (
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    ShardCommandRow,
    ShardGroupRow,
    session_scope,
)
from coire_api.instance.service import transition
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import node_admission_locks
from coire_core.models import (
    InstanceState,
    MemoryReservationState,
    ShardGroupCommand,
    ShardGroupState,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.shard_executor")


class ShardCommandExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="shard-command-executor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                command_id = await self._next_command()
            except Exception:
                logger.exception("shard command queue poll failed; retrying")
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), self.settings.placement_poll_interval_s
                    )
                continue
            if command_id is not None:
                await self._execute_safely(command_id)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.settings.placement_poll_interval_s)

    async def _next_command(self) -> uuid.UUID | None:
        async with session_scope() as session:
            return cast(
                uuid.UUID | None,
                await session.scalar(
                    select(ShardCommandRow.id)
                    .where(ShardCommandRow.state.in_(["pending", "running"]))
                    .order_by(ShardCommandRow.created_at)
                    .limit(1)
                ),
            )

    async def _execute_safely(self, command_id: uuid.UUID) -> None:
        with tracer.start_as_current_span("coire.sharding.command") as span:
            span.set_attribute("command_id", str(command_id))
            try:
                result = await self._execute(command_id)
            except NodeError as exc:
                span.record_exception(exc)
                if exc.retryable:
                    await asyncio.sleep(self.settings.placement_poll_interval_s)
                    return
                await self._failed(command_id, exc)
                return
            except Exception as exc:
                span.record_exception(exc)
                await self._failed(command_id, exc)
                return
        async with session_scope() as session:
            row = await session.get(ShardCommandRow, command_id)
            if row is not None:
                row.state = "succeeded"
                row.result = result
                row.updated_at = datetime.now(UTC)
        if result.get("state") == ShardGroupState.STOPPED.value:
            await self._finalize_stop(command_id)

    async def _finalize_stop(self, command_id: uuid.UUID) -> None:
        async with session_scope() as session:
            command = await session.get(ShardCommandRow, command_id)
            if command is None:
                return
            incomplete = await session.scalar(
                select(ShardCommandRow.id)
                .where(
                    ShardCommandRow.group_id == command.group_id,
                    ShardCommandRow.operation == "stop",
                    ShardCommandRow.state != "succeeded",
                )
                .limit(1)
            )
            if incomplete is not None:
                return
            group = await session.get(ShardGroupRow, command.group_id)
            if group is None:
                return
            members = list(
                (
                    await session.execute(
                        select(InstanceMemberRow).where(
                            InstanceMemberRow.instance_id == group.instance_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            async with node_admission_locks(session, [member.node_id for member in members]):
                for member in members:
                    member.rank_healthy = False
                    if member.reservation_id is not None:
                        reservation = await session.get(MemoryReservationRow, member.reservation_id)
                        if reservation is not None:
                            reservation.state = MemoryReservationState.RELEASED
                            reservation.released_at = datetime.now(UTC)
                instance = await session.get(ModelInstanceRow, group.instance_id)
                if group.state is ShardGroupState.STOPPING:
                    group.state = ShardGroupState.STOPPED
                    group.stopped_at = datetime.now(UTC)
                    if instance is not None and instance.state is not InstanceState.STOPPED:
                        await transition(
                            session,
                            instance.id,
                            InstanceState.STOPPED,
                            reason="both rank stops confirmed",
                        )

    async def _failed(self, command_id: uuid.UUID, exc: Exception) -> None:
        async with session_scope() as session:
            row = await session.get(ShardCommandRow, command_id)
            if row is not None:
                row.state = "failed"
                row.failure_code = type(exc).__name__.lower()[:64]
                row.failure_detail = "shard operation failed; inspect correlated logs"
                row.updated_at = datetime.now(UTC)
                group = await session.get(ShardGroupRow, row.group_id)
                if group is not None:
                    group.state = ShardGroupState.FAILED
                    group.state_reason = row.failure_detail
                logger.error(
                    "shard command failed group_id=%s command_id=%s operation=%s failure_code=%s",
                    row.group_id,
                    command_id,
                    row.operation,
                    row.failure_code,
                )

    async def _execute(self, command_id: uuid.UUID) -> dict[str, object]:
        async with session_scope() as session:
            row = await session.get(ShardCommandRow, command_id)
            if row is None:
                raise RuntimeError("shard command disappeared")
            node = await session.get(NodeRow, row.node_id)
            if node is None:
                raise RuntimeError("shard command node disappeared")
            operation, payload, node_name, group_id = (
                row.operation,
                dict(row.payload),
                node.name,
                row.group_id,
            )
            row.state = "running"
        async with NodeClient(
            self.settings, timeout=self.settings.sharding_start_timeout_s
        ) as client:
            if operation == "prepare":
                result = await client.prepare_shard_group(
                    node_name, ShardGroupCommand.model_validate(payload)
                )
            elif operation == "ready":
                result = await client.mark_shard_group_ready(node_name, group_id)
            elif operation == "stop":
                result = await client.stop_shard_group(node_name, group_id)
            else:
                raise RuntimeError(f"unknown shard operation {operation}")
        logger.info(
            "shard command completed group_id=%s command_id=%s node=%s operation=%s state=%s",
            group_id,
            command_id,
            node_name,
            operation,
            result.state.value,
        )
        return result.model_dump(mode="json")
