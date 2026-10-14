"""Lost acknowledgments replay exact retained bytes after restart."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import UpgradePhase, UpgradeReceipt, UpgradeReceiptBatch
from coire_core.settings import Settings
from coire_core.upgrade_journal import UpgradeReceiptJournal
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.replay import replay_receipts


@pytest.mark.parametrize("fault", [None, "lost_ack", "ahead"])
async def test_replay_keeps_bytes_and_restarts_at_durable_cursor(
    tmp_path: Path, fault: str | None
) -> None:
    plan = core_plan()
    journal = UpgradeReceiptJournal(tmp_path / "receipts", plan)
    for sequence in range(1, 34):
        journal.append(
            UpgradeReceipt(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                sequence=sequence,
                effect_id=f"effect-{sequence}",
                phase=UpgradePhase.DRAINING,
                occurred_at=datetime.now(UTC),
                outcome="complete",
            )
        )
    calls: list[tuple[int, ...]] = []
    failed = False

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        batch = UpgradeReceiptBatch.model_validate_json(request.content)
        calls.append(tuple(item.sequence for item in batch.receipts))
        if fault == "lost_ack" and not failed:
            failed = True
            raise httpx.ReadTimeout("ack lost", request=request)
        return httpx.Response(
            200,
            json={
                "job_id": plan.operation.job_id,
                "generation": plan.operation.generation,
                "sequence": batch.receipts[-1].sequence + (1 if fault == "ahead" else 0),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        client = ExecutorClient(
            Settings(upgrade_executor_token=SecretStr("coire_upgrade_private-test")), client=http
        )
        if fault:
            with pytest.raises((UpgradeUnavailable, UpgradeConflict)):
                await replay_receipts(client, journal)
            assert journal.acknowledged_sequence() == 0
        if fault != "ahead":
            restarted = UpgradeReceiptJournal(tmp_path / "receipts", plan)
            assert await replay_receipts(client, restarted) == 33
            count = len(calls)
            assert await replay_receipts(client, restarted) == 33
            assert len(calls) == count
        assert journal.latest() is not None
        assert len(list((tmp_path / "receipts").glob("receipt-*.json"))) == 33
        if fault == "lost_ack":
            assert calls[0] == calls[1] == tuple(range(1, 33))
