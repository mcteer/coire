"""Fresh fences and staging receipts do not replace live ownership checks before Core effects."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import core_plan

from coire_api.upgrades import grants
from coire_api.upgrades import inventory as inventory_service
from coire_api.upgrades.admission import CoreAdmissions
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    CoreUpgradeInventory,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "busy_core",
        "unknown_core",
        "busy_node",
        "foreign_core",
        "foreign_node",
        "stale",
        "future",
        "bootstrap",
        "missing_owner",
    ],
)
async def test_core_grants_require_every_affected_owner_idle_and_fresh(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    value = core_plan()
    now = datetime.now(UTC)
    timestamp = (
        now + timedelta(seconds=1)
        if fault == "future"
        else now - timedelta(seconds=6)
        if fault == "stale"
        else now
    )
    owners = tuple(
        UpgradeOwnerInventory(
            owner=name,
            status="unknown" if fault == "unknown_core" and name == "scheduler" else "complete",
            reason="owner_unavailable" if fault == "unknown_core" and name == "scheduler" else None,
            entries=(
                UpgradeInventoryEntry(
                    owner=name, kind="training", work_id="owned-work", state="active"
                ),
            )
            if fault == "busy_core" and name == "scheduler"
            else (),
        )
        for name in sorted(inventory_service.core_required_owners(value))
    )
    snapshot = CoreUpgradeInventory(
        job_id=value.operation.job_id,
        generation=value.operation.generation,
        project="other" if fault == "foreign_core" else "coire",
        release_sha256=value.operation.source_sha256,
        observed_at=timestamp,
        owners=owners,
    )
    observed = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(inventory_service, "core_inventory", observed)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def status(node):
        return NodeUpgradeStatus(
            node=node,
            runtime_sha256="b" * 64,
            release_sha256="c" * 64,
            observed_at=timestamp,
            bootstrap_ready=fault != "bootstrap",
        )

    async def inventory(node, job_id, generation):
        return NodeUpgradeInventory(
            node=node,
            job_id=job_id,
            generation=generation,
            runtime_sha256="f" * 64 if fault == "foreign_node" else "b" * 64,
            observed_at=timestamp,
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=name,
                    status="complete",
                    entries=(
                        UpgradeInventoryEntry(
                            owner=name, kind="training", work_id="owned-work", state="cleaning"
                        ),
                    )
                    if fault == "busy_node" and name == "training"
                    else (),
                )
                for name in sorted(NODE_UPGRADE_OWNERS)
            ),
        )

    client.upgrade_status.side_effect = status
    client.upgrade_inventory.side_effect = inventory
    monkeypatch.setattr("coire_api.nodes_client.NodeClient", lambda *_args, **_kwargs: client)
    admissions = None if fault == "missing_owner" else CoreAdmissions(enabled=True)
    if fault:
        with pytest.raises(UpgradeUnavailable if fault == "missing_owner" else UpgradeConflict):
            await grants.verify_live_core_drain(
                AsyncMock(), value, Settings(upgrades_enabled=True), admissions
            )
    else:
        await grants.verify_live_core_drain(
            AsyncMock(), value, Settings(upgrades_enabled=True), admissions
        )
        assert client.upgrade_inventory.await_count == 2
        assert client.upgrade_status.await_count == 2
    # These checks only observe owners; they never cancel or stop user work.
    client.cancel_upgrade.assert_not_awaited()
    client.apply_upgrade_maintenance.assert_not_awaited()
