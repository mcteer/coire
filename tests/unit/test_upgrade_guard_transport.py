"""Independent guard uses bounded existing authenticated node control routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from upgrade_fixtures import plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NodeUpgradeInventory,
    UpgradeOwnerInventory,
)
from coire_core.settings import Settings
from coire_node.maintenance import REQUIRED_OWNERS


@pytest.mark.parametrize(
    "fault", [None, "wrong_node", "wrong_digest", "stale", "oversize", "redirect"]
)
def test_guard_accepts_only_fresh_bounded_fixed_node_observations(fault: str | None) -> None:
    from coire_node.upgrade_guard import NativeGuardTransport

    value = plan()
    assert value.source.manifest.environment_sha256 is not None
    seen: list[httpx.Request] = []
    snapshot = NodeUpgradeInventory(
        job_id=value.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=value.source.manifest.environment_sha256,
        observed_at=datetime.now(UTC) - timedelta(seconds=10 if fault == "stale" else 0),
        owners=tuple(
            UpgradeOwnerInventory(owner=name, status="complete") for name in sorted(REQUIRED_OWNERS)
        ),
    )
    if fault == "wrong_node":
        snapshot = snapshot.model_copy(update={"node": "coire-edge-b"})
    elif fault == "wrong_digest":
        snapshot = snapshot.model_copy(update={"runtime_sha256": "f" * 64})

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * (512 * 1024 + 1))
        if fault == "redirect":
            return httpx.Response(307, headers={"Location": "http://foreign/secret"})
        return httpx.Response(200, content=snapshot.model_dump_json())

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="coire-edge-a.lab",
        node_token=SecretStr("private-test-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(reply), trust_env=False, follow_redirects=False
    ) as client:
        owner = NativeGuardTransport(settings, client)
        if fault:
            with pytest.raises(UpgradeUnavailable):
                owner.inventory(value, value.source.manifest.environment_sha256)
        else:
            assert owner.inventory(value, value.source.manifest.environment_sha256).idle(
                REQUIRED_OWNERS
            )
    assert len(seen) == 1
    assert seen[0].url.host == "coire-edge-a.lab"
    assert seen[0].url.path == f"/node/maintenance/{value.operation.job_id}/inventory"
    assert seen[0].headers["Authorization"] == "Bearer private-test-token"


def test_guard_missing_credentials_does_not_contact_node() -> None:
    from coire_node.upgrade_guard import NativeGuardTransport

    client = Mock()
    with pytest.raises(UpgradeUnavailable):
        NativeGuardTransport(
            Settings(node_name="coire-edge-a", node_control_host="coire-edge-a.lab"), client
        )
    client.stream.assert_not_called()


def test_guard_refreshes_peer_only_through_scoped_core_callback() -> None:
    from coire_core.models.upgrades import NodeUpgradePeerRead
    from coire_node.upgrade_guard import NativeGuardTransport

    original = plan()
    value = original.model_copy(
        update={
            "peer_runtime_sha256": "c" * 64,
            "maintenance": original.maintenance.model_copy(
                update={"targets": ("coire-edge-a", "coire-edge-b")}
            ),
        }
    )
    snapshot = NodeUpgradeInventory(
        job_id=value.operation.job_id,
        generation=1,
        node="coire-edge-b",
        runtime_sha256="c" * 64,
        observed_at=datetime.now(UTC),
        owners=(),
    )
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert (
            NodeUpgradePeerRead.model_validate_json(request.content).plan_sha256
            == value.authority_sha256()
        )
        return httpx.Response(200, content=snapshot.model_dump_json())

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="coire-edge-a.lab",
        core_control_host="coire-core.lab",
        node_token=SecretStr("private-test-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(reply), trust_env=False, follow_redirects=False
    ) as client:
        owner = NativeGuardTransport(settings, client)
        assert (
            owner.peer_inventory(value, deadline_at=datetime.now(UTC) + timedelta(seconds=120))
            == snapshot
        )
    assert len(seen) == 1 and seen[0].url.host == "coire-core.lab"
    assert seen[0].url.path == f"/api/v1/internal/upgrades/{value.operation.job_id}/peer-inventory"
