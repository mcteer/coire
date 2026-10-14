"""Typed cleanup client is fixed rank zero and refuses a foreign receipt."""

from unittest.mock import AsyncMock

import pytest
from test_collective_cleanup_inventory import inventories
from test_upgrade_collective import scope

from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.upgrades import NodeUpgradeCollectiveCleanup
from coire_core.settings import Settings
from coire_node.collective_cleanup import verify_cleanup_inventories


@pytest.mark.parametrize("foreign", [False, True])
async def test_cleanup_client_binds_receipt_to_exact_scope(foreign):
    value = scope()
    receipt = verify_cleanup_inventories(value, *inventories(value))
    if foreign:
        receipt = receipt.model_copy(update={"scope_sha256": "f" * 64})
    client = NodeClient(Settings())
    client._call = AsyncMock(return_value=(200, receipt.model_dump(mode="json")))
    request = NodeUpgradeCollectiveCleanup(scope=value)
    if foreign:
        with pytest.raises(NodeError):
            await client.cleanup_upgrade_collective(request)
    else:
        assert await client.cleanup_upgrade_collective(request) == receipt
    client._call.assert_awaited_once_with(
        "POST",
        "coire-edge-a",
        f"/node/upgrades/{value.plan.operation.job_id}/collective-cleanup",
        json=request.model_dump(mode="json"),
        request_timeout_s=10.0,
        expect=(200,),
    )
