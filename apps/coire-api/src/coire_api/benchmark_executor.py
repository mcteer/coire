"""Sequential durable execution of append-only placement benchmarks."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import func, select

from coire_api.db import (
    BenchmarkCommandRow,
    BenchmarkRunRow,
    LinkObservationRow,
    NodeRow,
    PlacementBenchmarkRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models import BenchmarkCommand, BenchmarkRunState
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.benchmarks")
meter = metrics.get_meter("coire.api.benchmarks")
benchmark_runs = meter.create_counter("coire_sharding_benchmarks_total", unit="1")
benchmark_tps = meter.create_histogram(
    "coire_sharding_benchmark_tokens_per_second", unit="{token}/s"
)
logger = logging.getLogger(__name__)


class BenchmarkCommandExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="benchmark-command-executor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                command_id = await self._next()
            except Exception:
                logger.exception("benchmark command queue poll failed; retrying")
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

    async def _next(self) -> uuid.UUID | None:
        async with session_scope() as session:
            commands = list(
                (
                    await session.execute(
                        select(BenchmarkCommandRow)
                        .where(BenchmarkCommandRow.state.in_(["pending", "running"]))
                        .order_by(BenchmarkCommandRow.created_at, BenchmarkCommandRow.sequence)
                    )
                )
                .scalars()
                .all()
            )
            for command in commands:
                earlier = await session.scalar(
                    select(BenchmarkCommandRow.id)
                    .where(
                        BenchmarkCommandRow.run_id == command.run_id,
                        BenchmarkCommandRow.sequence < command.sequence,
                        BenchmarkCommandRow.state != "succeeded",
                    )
                    .limit(1)
                )
                if earlier is None:
                    return command.id
            return None

    async def _execute_safely(self, command_id: uuid.UUID) -> None:
        with tracer.start_as_current_span("coire.sharding.benchmark") as span:
            span.set_attribute("command_id", str(command_id))
            try:
                await self._execute(command_id)
            except NodeError as exc:
                span.record_exception(exc)
                if exc.retryable:
                    await asyncio.sleep(self.settings.placement_poll_interval_s)
                    return
                await self._fail(command_id, str(exc))
            except Exception as exc:
                span.record_exception(exc)
                await self._fail(command_id, type(exc).__name__)

    async def _execute(self, command_id: uuid.UUID) -> None:
        span = trace.get_current_span()
        span.set_attribute("command_id", str(command_id))
        async with session_scope() as session:
            row = await session.get(BenchmarkCommandRow, command_id)
            if row is None:
                return
            node = await session.get(NodeRow, row.node_id)
            run = await session.get(BenchmarkRunRow, row.run_id)
            if node is None or run is None:
                raise RuntimeError("benchmark state disappeared")
            row.state = "running"
            run.state = BenchmarkRunState.RUNNING
            command = BenchmarkCommand.model_validate(row.payload)
            span.set_attribute("run_id", str(command.run_id))
            span.set_attribute("placement", command.placement)
            node_name = node.name
        async with NodeClient(
            self.settings, timeout=self.settings.sharding_start_timeout_s
        ) as client:
            measurement = await client.run_benchmark(node_name, command)
        async with session_scope() as session:
            row = await session.get(BenchmarkCommandRow, command_id)
            run = await session.get(BenchmarkRunRow, command.run_id)
            nodes = list((await session.execute(select(NodeRow).order_by(NodeRow.name))).scalars())
            latest = await session.scalar(
                select(LinkObservationRow).order_by(LinkObservationRow.observed_at.desc()).limit(1)
            )
            if row is None or run is None:
                raise RuntimeError("benchmark state disappeared after execution")
            session.add(
                PlacementBenchmarkRow(
                    run_id=run.id,
                    variant_id=run.variant_id,
                    placement=measurement.placement,
                    tokens_per_second=measurement.tokens_per_second,
                    prompt_tokens=run.prompt_tokens,
                    generation_tokens=run.generation_tokens,
                    gpu_cores={node.name: node.gpu_cores or 0 for node in nodes},
                    os_versions=(
                        {
                            "coire-edge-a": latest.os_version_a,
                            "coire-edge-b": latest.os_version_b,
                        }
                        if latest is not None
                        else {}
                    ),
                    engine_version=measurement.engine_version,
                    failure=measurement.failure,
                )
            )
            row.state = "succeeded"
            row.updated_at = datetime.now(UTC)
            await session.flush()
            remaining = await session.scalar(
                select(func.count(BenchmarkCommandRow.id)).where(
                    BenchmarkCommandRow.run_id == run.id,
                    BenchmarkCommandRow.state != "succeeded",
                )
            )
            if not remaining:
                run.state = BenchmarkRunState.COMPLETED
                run.finished_at = datetime.now(UTC)
                benchmark_runs.add(1, {"outcome": "completed"})
            if measurement.tokens_per_second is not None:
                benchmark_tps.record(
                    measurement.tokens_per_second, {"placement": measurement.placement}
                )
            logger.info(
                "benchmark placement completed run_id=%s command_id=%s placement=%s tokens_per_second=%s",
                command.run_id,
                command_id,
                measurement.placement,
                measurement.tokens_per_second,
            )

    async def _fail(self, command_id: uuid.UUID, detail: str) -> None:
        async with session_scope() as session:
            row = await session.get(BenchmarkCommandRow, command_id)
            if row is None:
                return
            row.state = "failed"
            row.failure_detail = detail[:500]
            run = await session.get(BenchmarkRunRow, row.run_id)
            if run is not None:
                run.state = BenchmarkRunState.FAILED
                run.failure = "benchmark command failed; inspect correlated logs"
                run.finished_at = datetime.now(UTC)
                benchmark_runs.add(1, {"outcome": "failed"})
            logger.error(
                "benchmark command failed run_id=%s command_id=%s detail=%s",
                row.run_id,
                command_id,
                detail[:500],
            )
