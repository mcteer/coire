"""Single-flight cold engine loading using the existing node lifecycle."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from coire_api.db import EngineProcessRow, ModelCopyRow, ModelRow, NodeRow, session_scope
from coire_api.gateway.telemetry import tracer
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.engine import EngineState
from coire_core.models.node import Reachability
from coire_core.settings import Settings


class ModelLoadError(Exception):
    pass


class LoadCoordinator:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._loads: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def run(self, model_id: uuid.UUID, loader: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            task = self._loads.get(model_id)
            if task is None or task.done():
                task = asyncio.ensure_future(loader())
                self._loads[model_id] = task
        try:
            await task
        finally:
            async with self._lock:
                if self._loads.get(model_id) is task and task.done():
                    self._loads.pop(model_id, None)


coordinator = LoadCoordinator()


async def load_model(model_id: uuid.UUID, settings: Settings) -> None:
    async def _load() -> None:
        async with session_scope() as session:
            existing = await session.scalar(
                select(EngineProcessRow)
                .where(
                    EngineProcessRow.model_id == model_id,
                    EngineProcessRow.state.in_([EngineState.STARTING, EngineState.READY]),
                )
                .limit(1)
            )
            if existing is not None and existing.state is EngineState.READY:
                return
            model = await session.get(ModelRow, model_id)
            if model is None:
                raise ModelLoadError("model disappeared during load")
            target = (
                await session.execute(
                    select(ModelCopyRow, NodeRow)
                    .join(NodeRow, NodeRow.id == ModelCopyRow.node_id)
                    .where(
                        ModelCopyRow.model_id == model_id,
                        ModelCopyRow.verified.is_(True),
                        NodeRow.reachability == Reachability.HEALTHY,
                        NodeRow.last_observed_at
                        >= datetime.now(UTC) - timedelta(seconds=settings.node_health_freshness_s),
                    )
                    .order_by(NodeRow.name)
                    .limit(1)
                )
            ).one_or_none()
            if target is None:
                raise ModelLoadError("no verified reachable model copy")
            _, node = target
            row = existing or EngineProcessRow(
                id=uuid.uuid4(),
                model_id=model.id,
                node_id=node.id,
                port=0,
                state=EngineState.STARTING,
                estimate_bytes=model.memory_estimate_bytes,
            )
            if existing is None:
                session.add(row)
                await session.flush()
            try:
                async with NodeClient(settings, timeout=settings.gateway_wait_ceiling_s) as client:
                    _, engine = await client.start_engine(
                        node.name,
                        engine_id=row.id,
                        slug=model.slug,
                        estimate_bytes=model.memory_estimate_bytes,
                        chat_template=model.chat_template,
                    )
                    deadline = time.monotonic() + settings.gateway_wait_ceiling_s
                    while engine.state is EngineState.STARTING and time.monotonic() < deadline:
                        await asyncio.sleep(min(0.5, settings.node_engine_health_interval_s))
                        engine = await client.get_engine(node.name, row.id)
                    if engine.state is not EngineState.READY:
                        reason = engine.state_reason or f"engine entered {engine.state.value}"
                        raise ModelLoadError(reason)
            except (NodeError, ModelLoadError) as exc:
                row.state = EngineState.FAILED
                row.state_reason = str(exc)[:500]
                raise ModelLoadError(str(exc)) from exc
            row.port = engine.port
            row.pid = engine.pid
            row.process_create_time = engine.process_create_time
            row.state = engine.state
            row.state_reason = engine.state_reason
            row.load_seconds = engine.load_seconds

    with tracer.start_as_current_span("coire.node.load_wait") as span:
        span.set_attribute("coire.model.id", str(model_id))
        await coordinator.run(model_id, _load)
