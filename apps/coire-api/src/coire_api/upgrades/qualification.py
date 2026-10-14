"""Current qualification is bound to observed sealed Studio environments."""

from collections.abc import Sequence
from datetime import UTC, datetime

from coire_api.db import NodeRow
from coire_core.settings import Settings


def runtime_matches(
    nodes: Sequence[NodeRow],
    expected: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """Leave historical evidence intact while refusing unknown or changed runtimes."""
    if not settings.upgrades_enabled:
        return True
    observed_at = now or datetime.now(UTC)
    return bool(nodes) and all(
        node.name in {"coire-edge-a", "coire-edge-b"}
        and node.runtime_sha256 == expected
        and node.health_observed_at is not None
        and 0
        <= (observed_at - node.health_observed_at).total_seconds()
        <= settings.placement_health_freshness_s
        for node in nodes
    )


def harness_runtime_matches(
    node: NodeRow | None, expected: str | None, settings: Settings, *, now: datetime | None = None
) -> bool:
    """Legacy reports remain visible; only a current evaluated runtime grants Apply."""
    if not settings.upgrades_enabled:
        return True
    return bool(
        node is not None
        and expected is not None
        and runtime_matches([node], expected, settings, now=now)
    )


def training_runtimes_current(
    nodes: Sequence[NodeRow], settings: Settings, *, now: datetime | None = None
) -> bool:
    """Each rank needs fresh native identity; measured hardware binds its exact hash.

    Training's shared code digest is checked independently by every actual worker.
    A real pair may have different sealed interpreters and filesystem graphs.
    """
    if not settings.upgrades_enabled:
        return True
    return (
        bool(nodes)
        and len({node.name for node in nodes}) == len(nodes)
        and all(
            node.runtime_sha256 is not None
            and runtime_matches([node], node.runtime_sha256, settings, now=now)
            for node in nodes
        )
    )
