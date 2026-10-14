"""Local effects stay immutable, ordered and recoverable across lost acknowledgments."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    UpgradeExecutionPlan,
    UpgradeJournalEntry,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReceiptAck,
)
from coire_core.upgrade_journal import UpgradeReceiptJournal


def receipt(value: UpgradeExecutionPlan, sequence: int) -> UpgradeReceipt:
    return UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=value.operation.generation,
        sequence=sequence,
        effect_id="preflight",
        phase=UpgradePhase.PREFLIGHT,
        occurred_at=datetime.now(UTC),
        outcome="intent" if sequence == 1 else "complete",
    )


def test_restart_replay_and_lost_ack_keep_all_receipts(tmp_path: Path) -> None:
    value = plan()
    journal = UpgradeReceiptJournal(tmp_path / "receipts", value)
    first, second = receipt(value, 1), receipt(value, 2)
    assert journal.append(first)
    assert not journal.append(first)
    assert journal.append(second)
    restarted = UpgradeReceiptJournal(journal.store.root, value)
    assert restarted.page() == (first, second)
    restarted.acknowledge(
        UpgradeReceiptAck(job_id=value.operation.job_id, generation=1, sequence=1)
    )
    assert restarted.page() == (first, second)
    with pytest.raises(UpgradeConflict):
        restarted.acknowledge(
            UpgradeReceiptAck(job_id=value.operation.job_id, generation=1, sequence=0)
        )
    with pytest.raises(UpgradeConflict):
        restarted.acknowledge(
            UpgradeReceiptAck(job_id=value.operation.job_id, generation=1, sequence=3)
        )


def test_conflicting_effect_gap_and_corrupt_chain_fail_closed(tmp_path: Path) -> None:
    value = plan()
    journal = UpgradeReceiptJournal(tmp_path / "receipts", value)
    first = receipt(value, 1)
    journal.append(first)
    with pytest.raises(UpgradeConflict):
        journal.append(first.model_copy(update={"outcome": "failed"}))
    with pytest.raises(UpgradeConflict):
        journal.append(receipt(value, 3))
    journal.append(receipt(value, 2))
    entry = journal.store.read(journal.key(2), UpgradeJournalEntry)
    assert entry is not None
    journal.store.write(journal.key(2), entry.model_copy(update={"previous_sha256": "f" * 64}))
    with pytest.raises(UpgradeUnavailable):
        journal.page()
    with pytest.raises(UpgradeUnavailable):
        UpgradeReceiptJournal(journal.store.root, value)
