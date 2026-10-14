"""Replay retained receipts without renewing activation or discarding recovery evidence."""

import asyncio
from time import monotonic

from opentelemetry import trace

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradeReceiptBatch
from coire_core.upgrade_journal import UpgradeReceiptJournal

from .client import ExecutorClient
from .telemetry import logger, replay_duration, replays

tracer = trace.get_tracer("coire.upgrade_executor.replay")


async def replay_receipts(client: ExecutorClient, journal: UpgradeReceiptJournal) -> int:
    """One bounded replay attempt; failures leave the durable cursor and bytes intact."""
    client.bind(journal.plan)
    started, outcome = monotonic(), "incomplete"
    try:
        with tracer.start_as_current_span("coire.upgrade_executor.receipt_replay") as span:
            span.set_attribute("coire.job_id", journal.plan.operation.job_id)
            span.set_attribute("coire.upgrade_generation", journal.plan.operation.generation)
            # Subsequent attempts start from the fsynced acknowledgment.
            async with asyncio.timeout(10):
                while receipts := journal.page(journal.acknowledged_sequence()):
                    ack = await client.receipts(UpgradeReceiptBatch(receipts=receipts))
                    if ack.sequence != receipts[-1].sequence:
                        raise UpgradeConflict(
                            "Core acknowledged receipts outside the replayed page"
                        )
                    journal.acknowledge(ack)
            outcome = "complete"
            return journal.acknowledged_sequence()
    finally:
        replays.add(1, {"outcome": outcome})
        replay_duration.record(monotonic() - started, {"outcome": outcome})
        logger.info(
            "executor receipt replay finished",
            extra={
                "job_id": journal.plan.operation.job_id,
                "generation": journal.plan.operation.generation,
                "outcome": outcome,
            },
        )
