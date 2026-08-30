"""Choosing which node does what.

Policy, not a hard-coded host (ARCHITECTURE.md section 4). This feature exercises only
`single:*` and `pinned:*`; `sharded:*` is stored and validated but belongs to feature 006.

Nothing here evicts or reserves — that is feature 004's ledger. These functions answer "which
node is a candidate", and the caller still asks the node itself, which is the only party that
knows what it is really holding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from coire_core.models.node import Reachability

logger = logging.getLogger(__name__)

PREFERRED_NODE = "coire-edge-a"
"""Studio A holds the larger GPU (80 cores against 60) and is the default home for the largest
single-node model, so `single:auto` prefers it and falls back to B (feature 004 clarification)."""


class NoCandidate(RuntimeError):
    """No node can take this placement, with a reason worth showing an admin."""


@dataclass(frozen=True)
class NodeView:
    """What placement needs to know about a node. Assembled from the registry row plus the
    last status the prober received."""

    name: str
    reachability: Reachability
    store_free_bytes: int = 0
    memory_budget_bytes: int = 0
    memory_committed_bytes: int = 0

    @property
    def healthy(self) -> bool:
        return self.reachability is Reachability.HEALTHY

    @property
    def memory_free_bytes(self) -> int:
        return max(0, self.memory_budget_bytes - self.memory_committed_bytes)


def choose_origin(nodes: list[NodeView]) -> NodeView:
    """The Studio that pulls from Hugging Face: the one with the most free disk (spec FR-006).

    Ties break towards Studio A so the choice is deterministic — an arbitrary tie-break makes
    a failing acquisition hard to reproduce.
    """
    healthy = [n for n in nodes if n.healthy]
    if not healthy:
        raise NoCandidate("no node is reachable")
    return max(healthy, key=lambda n: (n.store_free_bytes, n.name == PREFERRED_NODE))


def replica_for(origin: NodeView, nodes: list[NodeView]) -> NodeView:
    """The peer that receives the copy. Every model lives on both Studios (Principle V)."""
    others = [n for n in nodes if n.name != origin.name and n.healthy]
    if not others:
        raise NoCandidate(
            f"{origin.name} is the only reachable node; a model is not ready until two "
            "verified copies exist"
        )
    return max(others, key=lambda n: n.store_free_bytes)


def choose_load_node(
    policy: str, estimate_bytes: int, nodes: list[NodeView], *, override: str | None = None
) -> NodeView:
    """Where to load a model.

    An explicit override wins, then a pinned or named placement, then `single:auto`. A named
    node that is unreachable is an error rather than a silent redirect: `pinned:coire-edge-b`
    means that node, and quietly loading elsewhere would break the reason it was pinned.
    """
    by_name = {n.name: n for n in nodes}

    def _require(name: str, why: str) -> NodeView:
        node = by_name.get(name)
        if node is None:
            raise NoCandidate(f"{why} names {name}, which is not a declared node")
        if not node.healthy:
            raise NoCandidate(f"{why} names {name}, which is {node.reachability.value}")
        return node

    if override:
        return _require(override, "the requested node")

    kind, _, target = policy.partition(":")
    if kind == "sharded":
        raise NoCandidate(
            f"placement {policy} needs sharded serving, which is feature 006; "
            "use a single-node placement"
        )
    if target and target != "auto":
        return _require(target, f"placement {policy}")

    healthy = [n for n in nodes if n.healthy]
    if not healthy:
        raise NoCandidate("no node is reachable")
    fitting = [n for n in healthy if n.memory_free_bytes >= estimate_bytes]
    if not fitting:
        # Not raised as "no candidate": the node makes the real admission decision and its
        # refusal carries the live figures. This is a hint, and the caller may still try.
        logger.info("no node reports room for %d bytes; trying the roomiest anyway", estimate_bytes)
        return max(healthy, key=lambda n: n.memory_free_bytes)
    # Prefer A, then whichever has the most headroom.
    return max(fitting, key=lambda n: (n.name == PREFERRED_NODE, n.memory_free_bytes))
