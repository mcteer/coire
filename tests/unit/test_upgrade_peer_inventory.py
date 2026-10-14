"""Peer reads are confined to a live accepted Studio owner and rechecked after I/O."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from upgrade_fixtures import plan

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import NodeUpgradeInventory, NodeUpgradePeerRead, UpgradePhase


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "wrong_requester",
        "wrong_generation",
        "wrong_plan",
        "terminal",
        "changed_owner",
        "stale_peer",
        "wrong_digest",
    ],
)
async def test_guard_can_read_only_its_current_fenced_peer(
    fault: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.upgrades import peer_inventory

    original = plan()
    value = original.model_copy(
        update={
            "peer_runtime_sha256": "c" * 64,
            "maintenance": original.maintenance.model_copy(
                update={"targets": ("coire-edge-a", "coire-edge-b")}
            ),
        }
    )
    operation = value.operation.model_copy(
        update={"phase": UpgradePhase.SUCCEEDED if fault == "terminal" else UpgradePhase.ACTIVATING}
    )
    if fault == "terminal":
        operation = operation.model_copy(update={"finished_at": datetime.now(UTC)})
    row = SimpleNamespace(
        payload=operation.model_dump(mode="json"), execution_plan=value.model_dump(mode="json")
    )
    lookup = AsyncMock(return_value=row)
    fence = AsyncMock()
    monkeypatch.setattr(peer_inventory, "get_operation", lookup)
    monkeypatch.setattr(peer_inventory, "verify_fenced", fence)
    client = Mock()
    snapshot = NodeUpgradeInventory(
        job_id=operation.job_id,
        generation=1,
        node="coire-edge-b",
        runtime_sha256="d" * 64 if fault == "wrong_digest" else "c" * 64,
        observed_at=datetime.now(UTC) - timedelta(seconds=10 if fault == "stale_peer" else 0),
        owners=(),
    )

    async def remote(*args: object) -> NodeUpgradeInventory:
        if fault == "changed_owner":
            row.payload = operation.model_copy(
                update={"phase": UpgradePhase.SUCCEEDED, "finished_at": datetime.now(UTC)}
            ).model_dump(mode="json")
        return snapshot

    client.upgrade_inventory = AsyncMock(side_effect=remote)
    request = NodeUpgradePeerRead(
        generation=2 if fault == "wrong_generation" else 1,
        plan_sha256="f" * 64 if fault == "wrong_plan" else value.authority_sha256(),
    )
    if fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            await peer_inventory.guard_peer_inventory(
                Mock(),
                "coire-edge-b" if fault == "wrong_requester" else "coire-edge-a",
                operation.job_id,
                request,
                client,
            )
        if fault in {"wrong_requester", "wrong_generation", "wrong_plan", "terminal"}:
            client.upgrade_inventory.assert_not_awaited()
    else:
        assert (
            await peer_inventory.guard_peer_inventory(
                Mock(), "coire-edge-a", operation.job_id, request, client
            )
            == snapshot
        )
        client.upgrade_inventory.assert_awaited_once_with("coire-edge-b", operation.job_id, 1)
        assert lookup.await_count == 2 and fence.await_count == 2
