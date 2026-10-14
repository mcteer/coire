"""Typed Core-to-node clients refuse stale owners and path-bearing identifiers."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.upgrades import NodeMaintenanceCommand, NodeUpgradeStatus, UpgradeMaintenance
from coire_core.settings import Settings

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def test_status_requires_exact_requested_node(monkeypatch: pytest.MonkeyPatch) -> None:
    client = NodeClient(Settings())
    status = NodeUpgradeStatus(node="coire-edge-b", observed_at=datetime.now(UTC))
    call = AsyncMock(return_value=(200, status.model_dump(mode="json")))
    monkeypatch.setattr(client, "_call", call)
    try:
        with pytest.raises(NodeError, match="identity differs"):
            await client.upgrade_status("coire-edge-a")
        with pytest.raises(ValidationError):
            await client.upgrade_status("coire-edge-a", "../other")
        assert call.await_count == 1
    finally:
        await client.aclose()


async def test_cancellation_resumes_at_current_receipt_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.models.upgrades import NodeUpgradeCancel, UpgradePhase

    client = NodeClient(Settings())
    status = NodeUpgradeStatus(
        node="coire-edge-a",
        job_id=JOB,
        generation=1,
        phase=UpgradePhase.VERIFYING,
        receipt_sequence=20,
        observed_at=datetime.now(UTC),
    )
    call = AsyncMock(return_value=(202, status.model_dump(mode="json")))
    monkeypatch.setattr(client, "_call", call)
    try:
        assert (
            await client.cancel_upgrade(
                "coire-edge-a", JOB, NodeUpgradeCancel(generation=1), after_sequence=19
            )
            == status
        )
        assert call.await_args.args[2].endswith("/cancel?after_sequence=19")
        with pytest.raises(ValueError):
            await client.cancel_upgrade(
                "coire-edge-a", JOB, NodeUpgradeCancel(generation=1), after_sequence=-1
            )
        call.return_value = (202, status.model_copy(update={"phase": None}).model_dump(mode="json"))
        with pytest.raises(NodeError, match="progress differs"):
            await client.cancel_upgrade("coire-edge-a", JOB, NodeUpgradeCancel(generation=1))
    finally:
        await client.aclose()


async def test_inventory_and_ack_require_current_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = NodeClient(Settings())
    now = datetime.now(UTC)
    maintenance = UpgradeMaintenance(
        job_id=JOB,
        generation=2,
        targets=("coire-edge-a",),
        state="draining",
        updated_at=now,
        drain_deadline_at=now,
    )
    call = AsyncMock(
        return_value=(
            200,
            {
                "job_id": JOB,
                "generation": 1,
                "node": "coire-edge-a",
                "applied_at": now.isoformat(),
                "released": False,
            },
        )
    )
    monkeypatch.setattr(client, "_call", call)
    try:
        with pytest.raises(NodeError, match="acknowledgment differs"):
            await client.apply_upgrade_maintenance(
                "coire-edge-a", NodeMaintenanceCommand(maintenance=maintenance)
            )
        call.return_value = (
            200,
            {
                "job_id": JOB,
                "generation": 1,
                "node": "coire-edge-a",
                "runtime_sha256": "a" * 64,
                "observed_at": now.isoformat(),
                "owners": [],
            },
        )
        with pytest.raises(NodeError, match="authority differs"):
            await client.upgrade_inventory("coire-edge-a", JOB, 2)
        with pytest.raises(ValueError):
            await client.upgrade_inventory("coire-edge-a", JOB, 0)
        assert call.await_count == 2
    finally:
        await client.aclose()
