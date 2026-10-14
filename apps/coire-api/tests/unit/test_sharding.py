from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from coire_core.models import LinkObservation, ProbeOutcome, ProbeTransport, RdmaState
from coire_scheduler.sharding import project_link, validate_hostfile


def observation(outcome: ProbeOutcome, at: datetime, latency: float = 5000.0) -> LinkObservation:
    return LinkObservation(
        id=uuid.uuid4(),
        node_a="coire-edge-a",
        node_b="coire-edge-b",
        transport=ProbeTransport.JACCL,
        outcome=outcome,
        bandwidth_bytes_per_second=1 if outcome is ProbeOutcome.SUCCEEDED else None,
        latency_ms=latency if outcome is ProbeOutcome.SUCCEEDED else None,
        os_version_a="15",
        os_version_b="15",
        engine_version="1",
        observed_at=at,
    )


def test_three_successes_admit_tp_regardless_of_latency() -> None:
    now = datetime.now(UTC)
    projection = project_link(
        [observation(ProbeOutcome.SUCCEEDED, now - timedelta(seconds=n)) for n in range(3)], now=now
    )
    assert projection.rdma_state is RdmaState.UP
    assert projection.tp_eligible


def test_two_failures_mark_rdma_down_and_stale_evidence_is_unknown() -> None:
    now = datetime.now(UTC)
    failed = [observation(ProbeOutcome.FAILED, now - timedelta(seconds=n)) for n in range(2)]
    assert project_link(failed, now=now).rdma_state is RdmaState.DOWN
    assert (
        project_link(
            [observation(ProbeOutcome.SUCCEEDED, now - timedelta(hours=1))], now=now
        ).rdma_state
        is RdmaState.UNKNOWN
    )


def test_hostfile_is_canonical_and_rejects_core_or_extra_nodes() -> None:
    payload = b'{"backend":"jaccl","hosts":[{"ssh":"coire-edge-a.fabric","ips":[],"rdma":[null,"rdma_en5"]},{"ssh":"coire-edge-b.fabric","ips":[],"rdma":["rdma_en5",null]}]}'
    digest = validate_hostfile(
        payload,
        {"coire-edge-a": "coire-edge-a.fabric", "coire-edge-b": "coire-edge-b.fabric"},
    )
    assert b"coire-core" not in payload and len(digest) == 64
    with pytest.raises(ValueError):
        validate_hostfile(
            payload,
            {"coire-edge-a": "coire-core.lab", "coire-edge-b": "coire-edge-b.fabric"},
        )


def test_managed_failed_collective_invalidates_older_ring_until_fresh_measurement() -> None:
    from coire_core.models import LinkState

    now = datetime.now(UTC)
    pair = ("a" * 64, "b" * 64)
    failure = observation(ProbeOutcome.FAILED, now).model_copy(
        update={
            "runtime_sha256_a": pair[0],
            "runtime_sha256_b": pair[1],
        }
    )
    old_ring = observation(ProbeOutcome.SUCCEEDED, now - timedelta(seconds=1)).model_copy(
        update={
            "transport": ProbeTransport.RING,
            "runtime_sha256_a": pair[0],
            "runtime_sha256_b": pair[1],
        }
    )
    assert (
        project_link([failure, old_ring], now=now, current_runtime_pair=pair).fallback_state
        is LinkState.DOWN
    )
    assert project_link([failure, old_ring], now=now).fallback_state is LinkState.UP
    new_ring = old_ring.model_copy(update={"observed_at": now + timedelta(seconds=1)})
    assert (
        project_link(
            [failure, new_ring], now=now + timedelta(seconds=1), current_runtime_pair=pair
        ).fallback_state
        is LinkState.UP
    )
