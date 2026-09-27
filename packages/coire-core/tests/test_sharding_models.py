from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from coire_core.models import (
    BenchmarkCommand,
    BenchmarkRequest,
    InstanceCreate,
    LinkObservation,
    PlacementRequest,
    ProbeOutcome,
    ProbeTransport,
    ShardGroupCommand,
    ShardingMode,
    ShardRank,
)


def test_sharded_policies_are_contract_values() -> None:
    variant_id = uuid.uuid4()
    assert PlacementRequest(variant_id=variant_id, policy="sharded:tp").policy == "sharded:tp"
    assert (
        InstanceCreate(model_id=uuid.uuid4(), variant_id=variant_id, policy="sharded:pp").policy
        == "sharded:pp"
    )


def test_link_observation_requires_canonical_pair_and_measurements() -> None:
    common: dict[str, Any] = {
        "id": uuid.uuid4(),
        "transport": ProbeTransport.JACCL,
        "outcome": ProbeOutcome.SUCCEEDED,
        "os_version_a": "15.6",
        "os_version_b": "15.6",
        "engine_version": "mlx-0.29",
        "observed_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError):
        LinkObservation(node_a="coire-edge-b", node_b="coire-edge-a", **common)
    with pytest.raises(ValidationError):
        LinkObservation(node_a="coire-edge-a", node_b="coire-edge-b", **common)


def test_group_command_rejects_caller_injected_rank_topology() -> None:
    rank = ShardRank(rank=0, node_name="coire-edge-a", host="edge-a.fabric", port=9600)
    with pytest.raises(ValidationError):
        ShardGroupCommand(
            command_id=uuid.uuid4(),
            group_id=uuid.uuid4(),
            instance_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
            slug="safe--registry-slug",
            mode=ShardingMode.TENSOR_PARALLEL,
            ranks=[rank, rank],
            estimate_bytes_per_rank=1024,
            hostfile_sha256="0" * 64,
        )


def test_benchmark_contract_requires_complete_order_and_generated_inventory() -> None:
    variant = uuid.uuid4()
    request = BenchmarkRequest(variant_id=variant)
    assert request.placements == ["single:coire-edge-a", "sharded:tp", "sharded:pp"]
    with pytest.raises(ValidationError):
        BenchmarkRequest(variant_id=variant, placements=["single:coire-edge-a"])
    with pytest.raises(ValidationError):
        BenchmarkCommand(
            command_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            variant_id=variant,
            slug="coire--model",
            placement="sharded:tp",
            prompt_tokens=1,
            generation_tokens=1,
        )
