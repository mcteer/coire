from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coire_core.models.node import Reachability
from coire_core.models.placement import LedgerUpdate, MemoryLedger, PlacementRequest


def test_ledger_requires_exact_derived_free_bytes() -> None:
    now = datetime.now(UTC)
    ledger = MemoryLedger(
        node_id=uuid.uuid4(),
        node_name="coire-edge-a",
        budget_bytes=100,
        sandbox_bytes=10,
        reserved_bytes=40,
        free_bytes=60,
        health=Reachability.HEALTHY,
        updated_at=now,
    )
    assert ledger.free_bytes == 60
    with pytest.raises(ValidationError, match="free_bytes"):
        MemoryLedger(
            node_id=uuid.uuid4(),
            node_name="coire-edge-a",
            budget_bytes=100,
            sandbox_bytes=10,
            reserved_bytes=40,
            free_bytes=61,
            health=Reachability.HEALTHY,
            updated_at=now,
        )


def test_budget_reduction_and_zero_sandbox_are_valid_updates() -> None:
    assert LedgerUpdate(budget_bytes=1).budget_bytes == 1
    assert LedgerUpdate(sandbox_bytes=0).sandbox_bytes == 0
    with pytest.raises(ValidationError, match="at least one"):
        LedgerUpdate()


def test_placement_policy_excludes_sharded_until_feature_006() -> None:
    variant = uuid.uuid4()
    assert PlacementRequest(variant_id=variant, policy="single:auto").policy == "single:auto"
    assert (
        PlacementRequest(variant_id=variant, policy="pinned:coire-edge-b").policy
        == "pinned:coire-edge-b"
    )
    with pytest.raises(ValidationError):
        PlacementRequest(variant_id=variant, policy="sharded:tp")
