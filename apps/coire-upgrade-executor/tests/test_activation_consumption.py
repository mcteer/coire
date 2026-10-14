"""An interrupted activation consume reuses its fsynced request and original grant."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    DeploymentOwnership,
    UpgradeActivationConsume,
    UpgradeActivationGrant,
    UpgradeReceiptAck,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


@pytest.mark.parametrize("fault", [None, "lost", "expired", "foreign_ack"])
async def test_consume_replays_exact_pending_intent_without_renewing_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = core_plan()
    journal = ExecutorJournal(tmp_path / "journal", plan)
    settings = Settings(upgrade_executor_token=SecretStr("coire_upgrade_test"))
    client, docker = ExecutorClient(settings), UpgradeDocker(settings, plan)
    now = datetime.now(UTC)
    grant = UpgradeActivationGrant(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    granted = AsyncMock(return_value=grant)
    monkeypatch.setattr(client, "grant", granted)
    consumed_requests: list[UpgradeActivationConsume] = []
    clock = Mock(wraps=datetime)

    async def consume(value: UpgradeActivationConsume) -> UpgradeReceiptAck:
        assert journal.store.read("consume-runtime.json", UpgradeActivationConsume) == value
        assert not journal.state().activations
        assert journal.receipts.latest() is None
        consumed_requests.append(value)
        if fault == "lost" and len(consumed_requests) == 1:
            raise UpgradeUnavailable("simulated lost consume reply")
        if fault == "expired":
            clock.now.return_value = grant.expires_at + timedelta(seconds=1)
            monkeypatch.setattr("coire_upgrade_executor.journal.datetime", clock)
            monkeypatch.setattr("coire_upgrade_executor.executor.datetime", clock)
        return UpgradeReceiptAck(
            job_id=value.intent.job_id,
            generation=value.intent.generation,
            sequence=value.intent.sequence + int(fault == "foreign_ack"),
        )

    consumer = AsyncMock(side_effect=consume)
    monkeypatch.setattr(client, "consume", consumer)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            if fault == "lost":
                with pytest.raises(UpgradeUnavailable):
                    await executor.consume_boundary()
                assert not journal.state().activations
                assert journal.receipts.acknowledged_sequence() == 0
            if fault == "foreign_ack":
                with pytest.raises(UpgradeConflict, match="acknowledgment"):
                    await executor.consume_boundary()
                assert not journal.state().activations
                assert journal.receipts.latest() is None
                return
            value = await executor.consume_boundary()
            assert journal.state().activations == (value,)
            assert journal.receipts.latest() == value.intent
            assert journal.receipts.acknowledged_sequence() == value.intent.sequence
            assert journal.state().recovery_captured_at == (
                grant.expires_at if fault == "expired" else None
            )
            assert await executor.consume_boundary() == value
            granted.assert_awaited_once_with(migration=False)
            assert consumer.await_count == (2 if fault == "lost" else 1)
            assert all(item == value for item in consumed_requests)
    finally:
        await client.close()
        await docker.close()
