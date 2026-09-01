from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coire_api.placement.service import drift_ratio
from coire_core.models.node import Reachability
from coire_scheduler.placement import NodeCapacity, node_admissible, plan_admission


def test_stale_or_unhealthy_node_blocks_admission() -> None:
    now = datetime.now(UTC)
    assert node_admissible(
        reachability=Reachability.HEALTHY,
        last_seen_at=now,
        now=now,
        freshness_seconds=30,
    )
    assert not node_admissible(
        reachability=Reachability.HEALTHY,
        last_seen_at=now - timedelta(seconds=31),
        now=now,
        freshness_seconds=30,
    )
    assert not node_admissible(
        reachability=Reachability.UNREACHABLE,
        last_seen_at=now,
        now=now,
        freshness_seconds=30,
    )


def test_budget_reduction_blocks_new_admission_without_releasing_holds() -> None:
    # No candidate is invented from measured free memory: the reservation total remains authority.
    capacity = NodeCapacity(budget_bytes=50, reserved_bytes=60)
    try:
        plan_admission(capacity, 1, [])
    except RuntimeError:
        pass
    else:
        raise AssertionError("over-budget ledger admitted new work")


def test_drift_is_measured_against_authoritative_reservations() -> None:
    assert drift_ratio(reserved_bytes=100, measured_bytes=110) == 0.1
    assert drift_ratio(reserved_bytes=0, measured_bytes=10) is None
    assert drift_ratio(reserved_bytes=100, measured_bytes=None) is None
