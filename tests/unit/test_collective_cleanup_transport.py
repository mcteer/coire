"""Cleanup observation uses fixed authenticated origins and one bounded read budget."""

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr
from test_collective_cleanup_inventory import inventories
from test_upgrade_collective import scope

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NodeUpgradeCollectiveCleanup
from coire_core.settings import Settings
from coire_node.upgrade_guard import NativeGuardTransport


@pytest.mark.parametrize("fault", [None, "unavailable", "physical"])
def test_cleanup_transport_fetches_canonical_snapshots_only(fault):
    value = scope()
    local, peer = inventories(value)
    if fault == "physical":
        peer = peer.model_copy(update={"owners": ()})  # Incomplete is never idle.
    calls = []

    def remote(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private-test-token"
        if len(calls) == 1:
            assert request.method == "GET"
            assert request.url.path == f"/node/maintenance/{value.plan.operation.job_id}/inventory"
            assert request.url.params["generation"] == str(value.request.generation)
            return httpx.Response(200, content=local.model_dump_json())
        assert request.method == "POST"
        assert request.url.path.endswith("/collective-cleanup-peer")
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert NodeUpgradeCollectiveCleanup.model_validate_json(request.content).scope == value
        return httpx.Response(
            503 if fault == "unavailable" else 200, content=peer.model_dump_json()
        )

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="127.0.0.1",
        node_token=SecretStr("private-test-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(remote), trust_env=False, follow_redirects=False
    ) as client:
        transport = NativeGuardTransport(settings, client)
        if fault:
            with pytest.raises(UpgradeUnavailable):
                transport.cleanup_inventories(NodeUpgradeCollectiveCleanup(scope=value))
        else:
            before = datetime.now(UTC)
            assert transport.cleanup_inventories(NodeUpgradeCollectiveCleanup(scope=value)) == (
                local,
                peer,
            )
            assert (datetime.now(UTC) - before).total_seconds() < 5
    assert len(calls) == 2
