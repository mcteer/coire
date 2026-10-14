"""Scheduler reconciliation keeps node epochs, receipts and commands immutable."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import NodeUpgradeStatus, UpgradePhase, UpgradeReceipt


async def test_receipt_reconciliation_refuses_wrong_epoch_before_ingestion(monkeypatch):
    from coire_scheduler import upgrades

    value = plan()
    ingest = AsyncMock()
    monkeypatch.setattr(upgrades, "ingest_receipt", ingest)
    status = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=value.operation.job_id,
        generation=2,
        observed_at=datetime.now(UTC),
    )
    with pytest.raises(UpgradeConflict):
        await upgrades.reconcile_receipts(AsyncMock(), value, status)
    ingest.assert_not_awaited()


async def test_receipt_reconciliation_ingests_contiguous_local_evidence(monkeypatch):
    from coire_scheduler import upgrades

    value = plan()
    receipt = UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.PREFLIGHT,
        effect_id="source-verified",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=value.operation.source_sha256,
    )
    ingest = AsyncMock()
    monkeypatch.setattr(upgrades, "ingest_receipt", ingest)
    status = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=value.operation.job_id,
        generation=1,
        observed_at=datetime.now(UTC),
        receipts=(receipt,),
        receipt_sequence=1,
    )
    session = AsyncMock()
    await upgrades.reconcile_receipts(session, value, status)
    assert ingest.await_args.args[:2] == (session, receipt)


async def test_saved_activation_delivery_never_issues_another_grant(monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from coire_core.models.upgrades import NodeUpgradeCommand, UpgradeActivationGrant
    from coire_scheduler import upgrades

    value = plan()
    now = datetime.now(UTC) - timedelta(seconds=60)
    grant = UpgradeActivationGrant(
        job_id=value.operation.job_id,
        generation=1,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    receipt = UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=1,
        sequence=7,
        phase=UpgradePhase.ACTIVATING,
        effect_id="runtime-activation",
        occurred_at=now,
        outcome="intent",
        observed_sha256=value.operation.source_sha256,
    )
    saved = NodeUpgradeCommand.model_validate(
        value.model_dump(mode="json")
        | {
            "authorization_expires_at": grant.expires_at,
            "activation_grant": grant,
            "activation_receipt": receipt,
        }
    )
    row = SimpleNamespace(
        payload=value.operation.model_copy(update={"phase": UpgradePhase.ACTIVATING}).model_dump(
            mode="json"
        ),
        node_command=saved.model_dump(mode="json"),
    )
    monkeypatch.setattr(upgrades, "operation_lock", AsyncMock())
    monkeypatch.setattr(upgrades, "get_operation", AsyncMock(return_value=row))
    issue = AsyncMock()
    monkeypatch.setattr(upgrades, "issue", issue)
    assert await upgrades.activation_command(AsyncMock(), value, None) == saved
    issue.assert_not_awaited()


def test_drain_inventory_requires_complete_exact_owners_and_fresh_epoch():
    from coire_core.models.upgrades import (
        NODE_UPGRADE_OWNERS,
        NodeUpgradeInventory,
        UpgradeOwnerInventory,
    )
    from coire_scheduler.upgrades import idle_inventory

    value = plan()
    snapshot = NodeUpgradeInventory(
        job_id=value.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=value.source.manifest.environment_sha256,
        observed_at=datetime.now(UTC),
        owners=tuple(
            UpgradeOwnerInventory(owner=name, status="complete") for name in NODE_UPGRADE_OWNERS
        ),
    )
    assert idle_inventory(value, snapshot, value.source.manifest.environment_sha256)
    assert not idle_inventory(
        value,
        snapshot.model_copy(update={"owners": snapshot.owners[:-1]}),
        value.source.manifest.environment_sha256,
    )
    with pytest.raises(UpgradeConflict):
        idle_inventory(
            value,
            snapshot.model_copy(update={"generation": 2}),
            value.source.manifest.environment_sha256,
        )
