"""Cleanup excludes only its exact logical hold, never physical/unknown owners."""

from datetime import UTC, datetime, timedelta

import pytest
from test_upgrade_collective import scope

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NodeUpgradeInventory,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)


def inventories(value):
    def snapshot(node, runtime, local):
        return NodeUpgradeInventory(
            job_id=value.plan.operation.job_id,
            generation=value.request.generation,
            node=node,
            runtime_sha256=runtime,
            observed_at=datetime.now(UTC),
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=name,
                    status="complete",
                    entries=(
                        UpgradeInventoryEntry(
                            owner="probes",
                            kind="upgrade-collective",
                            work_id=str(value.command_id),
                            state="cleaning",
                            holds=("unresolved-collective",),
                        ),
                    )
                    if local and name == "probes"
                    else (),
                )
                for name in sorted(NODE_UPGRADE_OWNERS)
            ),
        )

    return (
        snapshot("coire-edge-a", value.runtime_pair()[0], True),
        snapshot("coire-edge-b", value.runtime_pair()[1], False),
    )


def restored_b_scope():
    from uuid import NAMESPACE_URL, uuid5

    from coire_core.models.upgrades import NodeUpgradeCollectiveScope

    original = scope()
    plan = original.plan.model_copy(
        update={"operation": original.plan.operation.model_copy(update={"node": "coire-edge-b"})}
    )
    request = original.request.model_copy(update={"plan_sha256": plan.authority_sha256()})
    return NodeUpgradeCollectiveScope(
        plan=plan,
        request=request,
        index=original.index,
        command_id=uuid5(
            NAMESPACE_URL,
            f"coire-upgrade-collective/{plan.operation.job_id}/{request.stage}/{request.plan_sha256}/{original.index}",
        ),
    )


def test_cleanup_accepts_exact_restored_b_without_qualifying_it():
    from coire_node.collective_cleanup import verify_cleanup_inventories

    value = restored_b_scope()
    local, peer = inventories(value)
    peer = peer.model_copy(update={"runtime_sha256": value.plan.source.manifest.environment_sha256})
    receipt = verify_cleanup_inventories(value, local, peer)
    assert receipt.outcome == "interrupted"
    assert receipt.namespace_inventory_sha256s[peer.node] == peer.sha256()
    assert value.request.stage == "candidate"


def test_cleanup_is_not_probe_qualification():
    from coire_node.collective_cleanup import verify_cleanup_inventories

    value = scope()
    local, peer = inventories(value)
    receipt = verify_cleanup_inventories(value, local, peer)
    assert receipt.outcome == "interrupted"
    assert receipt.scope_sha256 == value.sha256()
    assert receipt.namespace_inventory_sha256s == {
        local.node: local.sha256(),
        peer.node: peer.sha256(),
    }
    assert local.owners != ()  # Original evidence is not rewritten or masked.


@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "physical",
        "foreign_hold",
        "stale",
        "future",
        "generation",
        "runtime",
        "missing_hold",
    ],
)
def test_uncertain_cleanup_refuses(fault):
    from coire_node.collective_cleanup import verify_cleanup_inventories

    value = scope()
    local, peer = inventories(value)
    if fault in {"unknown", "physical"}:
        owners = list(peer.owners)
        index = next(i for i, owner in enumerate(owners) if owner.owner == "probes")
        owners[index] = owners[index].model_copy(
            update={
                "status": "unknown" if fault == "unknown" else "complete",
                "entries": (
                    UpgradeInventoryEntry(
                        owner="probes", kind="native-process", work_id="worker", state="active"
                    ),
                )
                if fault == "physical"
                else (),
            }
        )
        peer = peer.model_copy(update={"owners": tuple(owners)})
    elif fault in {"foreign_hold", "missing_hold"}:
        owners = list(local.owners)
        index = next(i for i, owner in enumerate(owners) if owner.owner == "probes")
        entries = owners[index].entries
        owners[index] = owners[index].model_copy(
            update={
                "entries": ()
                if fault == "missing_hold"
                else (entries[0].model_copy(update={"work_id": "foreign"}),)
            }
        )
        local = local.model_copy(update={"owners": tuple(owners)})
    else:
        update = {
            "stale": {"observed_at": datetime.now(UTC) - timedelta(seconds=6)},
            "future": {"observed_at": datetime.now(UTC) + timedelta(seconds=1)},
            "generation": {"generation": peer.generation + 1},
            "runtime": {"runtime_sha256": "f" * 64},
        }[fault]
        peer = peer.model_copy(update=update)
    with pytest.raises(UpgradeUnavailable):
        verify_cleanup_inventories(value, local, peer)
