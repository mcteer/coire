"""Cleanup peer read cannot broaden the original qualification peer read."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_collective_cleanup_inventory import inventories, restored_b_scope
from test_upgrade_collective import scope

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import NodeUpgradeCollectiveCleanup, UpgradePhase


async def test_cleanup_peer_observes_exact_restored_b_without_qualification(monkeypatch):
    from coire_api.upgrades import collective_cleanup

    value = restored_b_scope()
    operation = value.plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    row = SimpleNamespace(
        payload=operation.model_dump(mode="json"), execution_plan=value.plan.model_dump(mode="json")
    )
    monkeypatch.setattr(collective_cleanup, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(collective_cleanup, "verify_fenced", AsyncMock())
    _, peer = inventories(value)
    peer = peer.model_copy(update={"runtime_sha256": value.plan.source.manifest.environment_sha256})
    client = Mock(upgrade_inventory=AsyncMock(return_value=peer))
    assert (
        await collective_cleanup.collective_cleanup_peer(
            Mock(commit=AsyncMock()),
            "coire-edge-a",
            operation.job_id,
            NodeUpgradeCollectiveCleanup(scope=value),
            client,
        )
        == peer
    )
    client.upgrade_inventory.assert_awaited_once_with(
        "coire-edge-b", operation.job_id, operation.generation
    )


@pytest.mark.parametrize(
    "fault",
    [None, "requester", "generation", "plan", "terminal", "changed", "runtime", "stale", "future"],
)
async def test_cleanup_peer_requires_current_exact_rank_zero_fence(fault, monkeypatch):
    from coire_api.upgrades import collective_cleanup

    value = scope()
    operation = value.plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    row = SimpleNamespace(
        payload=operation.model_dump(mode="json"), execution_plan=value.plan.model_dump(mode="json")
    )
    lookup = AsyncMock(return_value=row)
    fence = AsyncMock()
    monkeypatch.setattr(collective_cleanup, "get_operation", lookup)
    monkeypatch.setattr(collective_cleanup, "verify_fenced", fence)
    if fault == "generation":
        row.payload = operation.model_copy(
            update={"generation": operation.generation + 1}
        ).model_dump(mode="json")
    elif fault == "plan":
        row.execution_plan = None
    elif fault == "terminal":
        row.payload = operation.model_copy(
            update={"phase": UpgradePhase.ROLLED_BACK, "finished_at": datetime.now(UTC)}
        ).model_dump(mode="json")
    _, peer = inventories(value)
    if fault == "runtime":
        peer = peer.model_copy(update={"runtime_sha256": "f" * 64})
    elif fault in {"stale", "future"}:
        peer = peer.model_copy(
            update={
                "observed_at": datetime.now(UTC) + timedelta(seconds=-6 if fault == "stale" else 1)
            }
        )

    async def remote(*args):
        if fault == "changed":
            row.execution_plan = None
        return peer

    client = Mock()
    client.upgrade_inventory = AsyncMock(side_effect=remote)
    session = Mock(commit=AsyncMock())
    request = NodeUpgradeCollectiveCleanup(scope=value)
    if fault:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            await collective_cleanup.collective_cleanup_peer(
                session,
                "coire-edge-b" if fault == "requester" else "coire-edge-a",
                operation.job_id,
                request,
                client,
            )
        if fault in {"requester", "generation", "plan", "terminal"}:
            client.upgrade_inventory.assert_not_awaited()
    else:
        assert (
            await collective_cleanup.collective_cleanup_peer(
                session, "coire-edge-a", operation.job_id, request, client
            )
            == peer
        )
        client.upgrade_inventory.assert_awaited_once_with(
            "coire-edge-b", operation.job_id, operation.generation
        )
        assert lookup.await_count == fence.await_count == 2
        session.commit.assert_awaited_once()
