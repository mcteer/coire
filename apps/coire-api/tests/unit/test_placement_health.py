"""Freshness gates for placement candidates (observability FR-012f)."""

from datetime import UTC, datetime, timedelta

from coire_api.registry.placement import effective_reachability
from coire_core.models.node import Reachability


def test_fresh_health_remains_eligible() -> None:
    now = datetime.now(UTC)
    assert (
        effective_reachability(Reachability.HEALTHY, now - timedelta(seconds=29), 30, now=now)
        is Reachability.HEALTHY
    )


def test_stale_health_becomes_unknown() -> None:
    now = datetime.now(UTC)
    assert (
        effective_reachability(Reachability.HEALTHY, now - timedelta(seconds=31), 30, now=now)
        is Reachability.UNKNOWN
    )


def test_missing_observation_is_never_eligible() -> None:
    assert effective_reachability(Reachability.HEALTHY, None, 30) is Reachability.UNKNOWN
