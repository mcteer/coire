"""Own durable command executors in the scheduler process."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from opentelemetry import metrics, trace

from coire_api.benchmark_executor import BenchmarkCommandExecutor
from coire_api.placement.executor import PlacementCommandExecutor
from coire_api.registry.acquisition_executor import AcquisitionCommandExecutor
from coire_api.run_executor import RunCommandExecutor
from coire_api.run_reconciler import RunReconciliationCoordinator
from coire_api.shard_executor import ShardCommandExecutor
from coire_api.shard_reconciler import ShardReconciler
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
workers_running = metrics.get_meter("coire.scheduler.workers").create_gauge(
    "coire_scheduler_workers_running"
)
tracer = trace.get_tracer("coire.scheduler.workers")


class Worker(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class SchedulerWorkers:
    """Start once, unwind partial startup in reverse order, and stop cleanly."""

    def __init__(self, settings: Settings) -> None:
        self.shutdown_timeout_s = settings.scheduler_shutdown_timeout_s
        self.kill_executor = RunCommandExecutor(settings, external_kill_scan=True)
        self.workers: list[Worker] = [
            AcquisitionCommandExecutor(settings),
            PlacementCommandExecutor(settings),
            self.kill_executor,
            RunReconciliationCoordinator(settings),
            ShardCommandExecutor(settings),
            ShardReconciler(settings),
            BenchmarkCommandExecutor(settings),
        ]
        self.started: list[Worker] = []

    async def start(self) -> None:
        with tracer.start_as_current_span("coire.scheduler.workers.start"):
            try:
                for worker in self.workers:
                    await worker.start()
                    self.started.append(worker)
                    workers_running.set(len(self.started))
            except Exception:
                await self.stop()
                raise
        logger.info("scheduler workers started count=%d", len(self.started))

    async def stop(self) -> None:
        with tracer.start_as_current_span("coire.scheduler.workers.stop"):
            pending_workers = list(reversed(self.started))
            self.started.clear()
            tasks = [asyncio.create_task(worker.stop()) for worker in pending_workers]
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout_s)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.exception() is not None:
                        logger.error("scheduler worker stop failed: %s", task.exception())
                if pending:
                    logger.error("scheduler worker stop timed out count=%d", len(pending))
            workers_running.set(0)
