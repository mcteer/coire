"""Execute durable placement commands through the authenticated node client."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select

from coire_api.db import (
    EngineProcessRow,
    EvictionEventRow,
    MemoryReservationRow,
    NodeRow,
    PlacementCommandRow,
    PlacementDecisionRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.engine import EngineState
from coire_core.models.placement import MemoryReservationState, PlacementState
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class PlacementCommandExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="placement-command-executor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            command_id = await self._next_command()
            if command_id is not None:
                await self._execute_safely(command_id)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.placement_poll_interval_s
                )

    async def _next_command(self) -> uuid.UUID | None:
        async with session_scope() as session:
            command_id: uuid.UUID | None = await session.scalar(
                select(PlacementCommandRow.id)
                .where(PlacementCommandRow.state.in_(["pending", "running"]))
                .order_by(PlacementCommandRow.created_at)
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
            row = await session.get(PlacementCommandRow, command_id)
            if row is not None:
                row.state = "succeeded"
                row.result = result
                row.updated_at = datetime.now(UTC)
                if row.operation == "unload" and row.reservation_id is not None:
                    event = await session.scalar(
                        select(EvictionEventRow).where(
                            EvictionEventRow.decision_id == row.decision_id,
                            EvictionEventRow.reservation_id == row.reservation_id,
                        )
                    )
                    if event is not None:
                        event.outcome = "confirmed"

    async def _failed(self, command_id: uuid.UUID, exc: Exception) -> None:
        logger.exception("placement command %s failed", command_id, exc_info=exc)
        async with session_scope() as session:
            row = await session.get(PlacementCommandRow, command_id)
            if row is not None:
                row.state = "failed"
                row.failure_code = type(exc).__name__.lower()[:64]
                row.failure_detail = "node engine operation failed; inspect correlated logs"
                row.updated_at = datetime.now(UTC)

    async def _execute(self, command_id: uuid.UUID) -> dict[str, object]:
        async with session_scope() as session:
            row = await session.get(PlacementCommandRow, command_id)
            if row is None:
                raise RuntimeError("placement command disappeared")
            node = await session.get(NodeRow, row.node_id)
            engine = await session.get(EngineProcessRow, row.engine_id)
            if node is None:
                raise RuntimeError("placement command node disappeared")
            row.state = "running"
            row.updated_at = datetime.now(UTC)
            operation, payload, node_name = row.operation, dict(row.payload), node.name

        async with NodeClient(
            self.settings, timeout=self.settings.gateway_wait_ceiling_s
        ) as client:
            if operation == "unload":
                status = await client.stop_engine(node_name, row.engine_id)
                deadline = time.monotonic() + self.settings.gateway_wait_ceiling_s
                while (
                    status is not None
                    and status.state is not EngineState.STOPPED
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(self.settings.node_engine_health_interval_s)
                    status = await client.get_engine(node_name, row.engine_id)
                if status is not None and status.state is not EngineState.STOPPED:
                    raise RuntimeError("engine unload did not confirm before the deadline")
                async with session_scope() as session:
                    reservation = (
                        await session.get(MemoryReservationRow, row.reservation_id)
                        if row.reservation_id
                        else None
                    )
                    engine = await session.get(EngineProcessRow, row.engine_id)
                    if reservation is not None:
                        reservation.state = MemoryReservationState.RELEASED
                        reservation.released_at = datetime.now(UTC)
                    if engine is not None:
                        engine.state = EngineState.STOPPED
                        engine.stopped_at = datetime.now(UTC)
                    decision = await session.get(PlacementDecisionRow, row.decision_id)
                    if decision is not None and decision.policy == "idle-ttl":
                        decision.state = PlacementState.READY
                        decision.updated_at = datetime.now(UTC)
                    if row.reservation_id is not None:
                        event = await session.scalar(
                            select(EvictionEventRow).where(
                                EvictionEventRow.decision_id == row.decision_id,
                                EvictionEventRow.reservation_id == row.reservation_id,
                            )
                        )
                        if event is not None:
                            event.outcome = "confirmed"
                return {"state": status.state.value if status else "absent"}
            if operation == "load":
                _, status = await client.start_engine(
                    node_name,
                    engine_id=row.engine_id,
                    slug=str(payload["slug"]),
                    estimate_bytes=int(str(payload["estimate_bytes"])),
                    chat_template=(
                        str(payload["chat_template"])
                        if payload.get("chat_template") is not None
                        else None
                    ),
                )
                deadline = time.monotonic() + self.settings.gateway_wait_ceiling_s
                while status.state is EngineState.STARTING and time.monotonic() < deadline:
                    await asyncio.sleep(self.settings.node_engine_health_interval_s)
                    status = await client.get_engine(node_name, row.engine_id)
                if status.state is not EngineState.READY:
                    raise RuntimeError(
                        status.state_reason or f"engine entered {status.state.value}"
                    )
                async with session_scope() as session:
                    reservation = (
                        await session.get(MemoryReservationRow, row.reservation_id)
                        if row.reservation_id
                        else None
                    )
                    engine = await session.get(EngineProcessRow, row.engine_id)
                    if reservation is not None:
                        reservation.state = MemoryReservationState.HELD
                    if engine is not None:
                        engine.state = status.state
                        engine.port = status.port
                        engine.pid = status.pid
                        engine.process_create_time = status.process_create_time
                        engine.load_seconds = status.load_seconds
                return status.model_dump(mode="json")
        raise RuntimeError(f"unknown placement operation {operation}")
