"""Stable degraded-tier capability projection."""

from __future__ import annotations

from datetime import datetime

from coire_core.models.failover import FailoverStatus, FailoverTier

DEGRADED_UNAVAILABLE = (
    "conversation_persistence",
    "usage_accounting",
    "feedback",
    "admin",
    "model_acquisition",
    "training",
    "image_generation",
    "agent_runs",
    "mcp",
)
MINIMAL_UNAVAILABLE = (*DEGRADED_UNAVAILABLE, "sharded_inference")


def project_tier(
    *,
    elected_host: str | None,
    reachable: frozenset[str],
    snapshot_expires_at: datetime | None = None,
    full: bool = False,
) -> FailoverStatus:
    """Project the tier the caller is actually in, including an explicit denial list."""
    if full and elected_host is not None:
        tier = FailoverTier.FULL
        unavailable: tuple[str, ...] = ()
    elif elected_host is not None and len(reachable) >= 2:
        tier = FailoverTier.DEGRADED_INFERENCE
        unavailable = DEGRADED_UNAVAILABLE
    else:
        tier = FailoverTier.MINIMAL
        unavailable = MINIMAL_UNAVAILABLE
    return FailoverStatus(
        tier=tier,
        elected_host=elected_host,
        reachable_members=reachable,
        snapshot_expires_at=snapshot_expires_at,
        unavailable_capabilities=unavailable,
    )


def degraded_status(
    *, host: str | None = None, reachable: frozenset[str] = frozenset()
) -> FailoverStatus:
    """Describe the emergency inference tier without implying mutable control-plane authority."""
    if host is None:
        return project_tier(elected_host=None, reachable=reachable)
    return project_tier(elected_host=host, reachable=reachable or frozenset({host, "coire-edge-b"}))
