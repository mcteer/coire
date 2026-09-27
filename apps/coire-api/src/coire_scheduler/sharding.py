"""Deterministic sharding admission primitives; durable orchestration calls these functions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from coire_core.models import (
    LinkObservation,
    LinkState,
    ProbeOutcome,
    ProbeTransport,
    RdmaState,
    StudioLinkProjection,
)

STUDIO_NAMES = ("coire-edge-a", "coire-edge-b")


def project_link(
    observations: Sequence[LinkObservation],
    *,
    now: datetime | None = None,
    freshness_s: float = 120.0,
    failures_before_down: int = 2,
    successes_before_up: int = 3,
) -> StudioLinkProjection:
    """Project damped eligibility from append-only evidence.

    Bandwidth and latency are retained for operators but deliberately do not influence the
    verdict. TP needs current successful JACCL/RDMA evidence; PP may use the ring fallback.
    """
    current = now or datetime.now(UTC)
    ordered = sorted(observations, key=lambda item: item.observed_at, reverse=True)
    fresh = [
        item for item in ordered if current - item.observed_at <= timedelta(seconds=freshness_s)
    ]
    latest_transport = {
        transport: next((o for o in fresh if o.transport is transport), None)
        for transport in ProbeTransport
    }
    jaccl = latest_transport[ProbeTransport.JACCL]
    ring = latest_transport[ProbeTransport.RING]

    consecutive_successes = 0
    consecutive_failures = 0
    if jaccl is not None:
        target = jaccl.outcome
        for item in (o for o in fresh if o.transport is ProbeTransport.JACCL):
            if item.outcome is not target:
                break
            if target is ProbeOutcome.SUCCEEDED:
                consecutive_successes += 1
            else:
                consecutive_failures += 1

    rdma_state = RdmaState.UNKNOWN
    if consecutive_failures >= failures_before_down:
        rdma_state = RdmaState.DOWN
    elif consecutive_successes >= successes_before_up:
        rdma_state = RdmaState.UP
    elif jaccl is not None:
        rdma_state = RdmaState.DEGRADED
    fallback_state = (
        LinkState.UP
        if ring is not None and ring.outcome is ProbeOutcome.SUCCEEDED
        else LinkState.DOWN
    )
    ip_state = LinkState.UP if fresh else LinkState.UNKNOWN
    return StudioLinkProjection(
        node_a=STUDIO_NAMES[0],
        node_b=STUDIO_NAMES[1],
        ip_state=ip_state,
        rdma_state=rdma_state,
        fallback_state=fallback_state,
        tp_eligible=rdma_state is RdmaState.UP,
        required_after=current - timedelta(seconds=freshness_s),
        latest=ordered[:10],
        consecutive_successes=consecutive_successes,
        consecutive_failures=consecutive_failures,
        flapping=_is_flapping(fresh),
        reason=None if fresh else "no current link measurement",
    )


def _is_flapping(observations: Sequence[LinkObservation]) -> bool:
    outcomes = [item.outcome for item in observations if item.transport is ProbeTransport.JACCL]
    return sum(left is not right for left, right in pairwise(outcomes)) >= 2


def validate_hostfile(payload: bytes, data_hosts: dict[str, str]) -> str:
    """Validate complete ``mlx.distributed_config`` output against declared endpoints."""
    if set(data_hosts) != set(STUDIO_NAMES):
        raise ValueError("hostfile requires exactly the two declared Studios")
    expected = [data_hosts[name] for name in STUDIO_NAMES]
    if expected != ["coire-edge-a.fabric", "coire-edge-b.fabric"]:
        raise ValueError("invalid Studio data endpoints")
    try:
        document = json.loads(payload)
        entries = document["hosts"]
        hosts = [entry["ssh"] for entry in entries]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("invalid MLX hostfile") from exc
    if hosts != expected or len(entries) != 2 or any("core" in host for host in hosts):
        raise ValueError("hostfile does not contain exactly the declared Studios")
    if document.get("backend") not in {"jaccl", "ring"}:
        raise ValueError("unsupported distributed backend")
    if document["backend"] == "jaccl" and any("rdma" not in entry for entry in entries):
        raise ValueError("JACCL hostfile is missing generated RDMA device topology")
    return hashlib.sha256(payload).hexdigest()
