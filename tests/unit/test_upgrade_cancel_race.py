"""Core cancellation records follow the independent node's serialized decision."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import core_plan, plan

from coire_api.auth import Principal
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_api.upgrades import service
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NodeUpgradeStatus,
    UpgradeCancelRequest,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("outcome", ["committed", "cancelled", "lost"])
async def test_cancel_reconciles_node_commit_before_setting_core_intent(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import coire_api.nodes_client as nodes_client

    value = plan()
    row = SimpleNamespace(
        payload=value.operation.model_dump(mode="json"),
        execution_plan=value.model_dump(mode="json"),
    )
    monkeypatch.setattr(service, "operation_lock", AsyncMock())
    monkeypatch.setattr(service, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(service, "authorize", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(service, "write_principal_audit", audit)
    receipt = UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.SUCCEEDED,
        effect_id="existing-node-commit",
        occurred_at=datetime.now(UTC),
        outcome="complete",
    )
    status = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=value.operation.job_id,
        generation=1,
        phase=UpgradePhase.SUCCEEDED if outcome == "committed" else UpgradePhase.ACCEPTED,
        observed_at=datetime.now(UTC),
        receipts=(receipt,) if outcome == "committed" else (),
        receipt_sequence=1 if outcome == "committed" else 0,
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.cancel_upgrade.return_value = status
    if outcome == "lost":
        client.cancel_upgrade.side_effect = NodeError(NodeErrorKind.UNAVAILABLE, "coire-edge-a")
    monkeypatch.setattr(nodes_client, "NodeClient", lambda *_args, **_kwargs: client)

    async def ingest(*_args):
        assert row.payload["cancel_requested"] is False
        row.payload = value.operation.model_copy(
            update={
                "phase": UpgradePhase.SUCCEEDED,
                "finished_at": datetime.now(UTC),
                "receipt_sequence": 1,
            }
        ).model_dump(mode="json")

    monkeypatch.setattr(service, "ingest_receipt", ingest)
    if outcome == "lost":
        with pytest.raises(UpgradeUnavailable):
            await service.request_cancel(
                AsyncMock(),
                Principal(),
                value.operation.job_id,
                UpgradeCancelRequest(),
                settings=Settings(),
            )
        assert row.payload["cancel_requested"] is False
        audit.assert_not_awaited()
        return
    result = await service.request_cancel(
        AsyncMock(),
        Principal(),
        value.operation.job_id,
        UpgradeCancelRequest(),
        settings=Settings(),
    )
    assert result.cancel_requested is (outcome == "cancelled")
    if outcome == "committed":
        assert result.phase is UpgradePhase.SUCCEEDED
    assert client.cancel_upgrade.await_args.kwargs["after_sequence"] == 0
    audit.assert_awaited_once()


@pytest.mark.parametrize("decided", [False, True])
async def test_core_cancellation_stops_at_the_acknowledged_selection_boundary(
    monkeypatch: pytest.MonkeyPatch, decided: bool
) -> None:
    value = core_plan()
    operation = value.operation.model_copy(
        update={"phase": UpgradePhase.COMMITTING, "receipt_sequence": 1}
    )
    row = SimpleNamespace(payload=operation.model_dump(mode="json"))
    decision = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="core-selection-decision",
        phase=UpgradePhase.COMMITTING,
        occurred_at=datetime.now(UTC),
        outcome="complete" if decided else "intent",
    )
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(payload=decision.model_dump(mode="json"))
    monkeypatch.setattr(service, "operation_lock", AsyncMock())
    monkeypatch.setattr(service, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(service, "authorize", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(service, "write_principal_audit", audit)
    if decided:
        with pytest.raises(UpgradeConflict, match="cancellation boundary"):
            await service.request_cancel(
                session, Principal(), operation.job_id, UpgradeCancelRequest()
            )
        assert row.payload["cancel_requested"] is False
        audit.assert_not_awaited()
    else:
        cancelled = await service.request_cancel(
            session, Principal(), operation.job_id, UpgradeCancelRequest()
        )
        assert cancelled.cancel_requested
        audit.assert_awaited_once()
