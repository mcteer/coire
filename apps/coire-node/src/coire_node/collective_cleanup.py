"""Interrupted collective cleanup proof; never dispatches or qualifies a probe."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NodeUpgradeCollectiveCleanup,
    NodeUpgradeCollectiveCleanupReceipt,
    NodeUpgradeCollectiveScope,
    NodeUpgradeInventory,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.maintenance import NodeMaintenance

tracer = trace.get_tracer("coire.node.upgrades")
logger = logging.getLogger(__name__)
cleanup_outcomes = metrics.get_meter("coire.node.upgrades").create_counter(
    "coire_upgrade_collective_cleanup_total",
    description="Scoped interrupted collective cleanup outcomes",
)


def verify_cleanup_inventories(
    scope: NodeUpgradeCollectiveScope,
    local: NodeUpgradeInventory,
    peer: NodeUpgradeInventory,
    *,
    observed_at: datetime | None = None,
) -> NodeUpgradeCollectiveCleanupReceipt:
    """Validate producer-fetched canonical evidence, not caller assertions.

    The producer must separately hold the retained scope's exclusive dispatch
    lock and check the current fence. This function alone cannot close a journal.
    """
    scope = NodeUpgradeCollectiveScope.model_validate(scope.model_dump(mode="json"))
    now = observed_at or datetime.now(UTC)
    if now.tzinfo is None or now > datetime.now(UTC):
        raise UpgradeUnavailable("Collective cleanup observation time differs")
    for snapshot, node in ((local, "coire-edge-a"), (peer, "coire-edge-b")):
        if (snapshot.job_id, snapshot.generation, snapshot.node) != (
            scope.plan.operation.job_id,
            scope.request.generation,
            node,
        ) or not 0 <= (now - snapshot.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Collective cleanup inventory authority or freshness differs")
    local_runtimes = {scope.runtime_pair()[0]}
    if scope.plan.operation.node == "coire-edge-a":
        source_runtime = scope.plan.source.manifest.environment_sha256
        if source_runtime is None:
            raise UpgradeUnavailable("Collective cleanup source runtime is unavailable")
        local_runtimes.add(source_runtime)
    peer_runtimes = {scope.runtime_pair()[1]}
    if scope.plan.operation.node == "coire-edge-b":
        source_runtime = scope.plan.source.manifest.environment_sha256
        if source_runtime is None:
            raise UpgradeUnavailable("Collective cleanup source runtime is unavailable")
        peer_runtimes.add(source_runtime)
    if local.runtime_sha256 not in local_runtimes or peer.runtime_sha256 not in peer_runtimes:
        raise UpgradeUnavailable("Collective cleanup runtime identity differs")
    matched = 0
    filtered = []
    for owner in local.owners:
        entries = []
        for entry in owner.entries:
            if (
                owner.owner == "probes"
                and entry.kind == "upgrade-collective"
                and entry.work_id == str(scope.command_id)
                and entry.state == "cleaning"
                and entry.holds == ("unresolved-collective",)
                and entry.process_id is None
                and entry.process_created_at is None
            ):
                matched += 1
            else:
                entries.append(entry)
        filtered.append(owner.model_copy(update={"entries": tuple(entries)}))
    if (
        matched != 1
        or not local.model_copy(update={"owners": tuple(filtered)}).idle(NODE_UPGRADE_OWNERS)
        or not peer.idle(NODE_UPGRADE_OWNERS)
    ):
        raise UpgradeUnavailable("Collective cleanup retains physical or unknown ownership")
    return NodeUpgradeCollectiveCleanupReceipt(
        job_id=scope.plan.operation.job_id,
        generation=scope.request.generation,
        command_id=scope.command_id,
        scope_sha256=scope.sha256(),
        namespace_inventory_sha256s={local.node: local.sha256(), peer.node: peer.sha256()},
        observed_at=now,
    )


def retained_cleanup_receipt(
    root: PrivateUpgradeStore,
    scope: NodeUpgradeCollectiveScope,
) -> NodeUpgradeCollectiveCleanupReceipt | None:
    """Validate immutable closure against its retained canonical preimages."""
    receipt = root.read("cleanup.json", NodeUpgradeCollectiveCleanupReceipt)
    if receipt is None:
        return None
    local = root.read("cleanup-local.json", NodeUpgradeInventory)
    peer = root.read("cleanup-peer.json", NodeUpgradeInventory)
    if local is None or peer is None:
        raise UpgradeUnavailable("Collective cleanup retained evidence is incomplete")
    expected = verify_cleanup_inventories(scope, local, peer, observed_at=receipt.observed_at)
    if receipt != expected:
        raise UpgradeUnavailable("Collective cleanup retained evidence authority differs")
    return receipt


def _reconcile_collective_cleanup(
    settings: Settings,
    body: NodeUpgradeCollectiveCleanup,
    gate: NodeMaintenance,
    fetch: Callable[[], tuple[NodeUpgradeInventory, NodeUpgradeInventory]],
) -> NodeUpgradeCollectiveCleanupReceipt:
    """Close only an interrupted dispatch after trusted canonical observation.

    `fetch` belongs to the authenticated transport producer, never a wire field.
    No process is started or signalled here. No measured result is manufactured.
    """
    body = NodeUpgradeCollectiveCleanup.model_validate(body.model_dump(mode="json"))
    scope = body.scope
    if (
        not settings.upgrades_enabled
        or settings.node_name != "coire-edge-a"
        or gate.node != settings.node_name
        or gate.store.root != Path(settings.node_state_dir) / "upgrades"
    ):
        raise UpgradeUnavailable("Collective cleanup requires declared rank-zero ownership")

    def verify_fence() -> None:
        with gate.lock:
            current = gate.current()
            if (
                current is None
                or current.state == "released"
                or not current.matches_plan(scope.plan)
            ):
                raise UpgradeConflict("Collective cleanup maintenance authority differs")

    root = gate.store
    for part in ("collectives", scope.plan.operation.job_id, scope.request.stage, str(scope.index)):
        root = PrivateUpgradeStore(root.path(part))
    # Do not hold gate.lock over transport I/O: canonical inventory needs it.
    # The filesystem fence lock pins cross-process maintenance effect authority.
    with gate.store.ownership("maintenance.lock"), root.ownership("collective.lock"):
        verify_fence()
        previous = root.read("scope.json", NodeUpgradeCollectiveScope)
        if previous != scope:
            raise UpgradeConflict("Collective cleanup differs from retained dispatch")
        if root.read("result.json", NodeUpgradeCollectiveResult) is not None:
            raise UpgradeConflict("Collective dispatch already has a worker result")
        receipt = retained_cleanup_receipt(root, scope)
        if receipt is not None:
            return receipt
        local, peer = fetch()
        receipt = verify_cleanup_inventories(scope, local, peer)
        verify_fence()
        # Until cleanup.json is durable, these are unconsumed observations;
        # retry may replace them with fresh evidence after a partial write.
        root.write("cleanup-local.json", local)
        root.write("cleanup-peer.json", peer)
        root.write("cleanup.json", receipt, replace=False)
        return receipt


@tracer.start_as_current_span("coire.node.collective.cleanup", record_exception=False)
def reconcile_collective_cleanup(
    settings: Settings,
    body: NodeUpgradeCollectiveCleanup,
    gate: NodeMaintenance,
    fetch: Callable[[], tuple[NodeUpgradeInventory, NodeUpgradeInventory]],
) -> NodeUpgradeCollectiveCleanupReceipt:
    fields = {
        "job_id": body.scope.plan.operation.job_id,
        "generation": body.scope.request.generation,
        "command_id": str(body.scope.command_id),
    }
    try:
        receipt = _reconcile_collective_cleanup(settings, body, gate, fetch)
    except (CoireError, OSError, ValueError):
        cleanup_outcomes.add(1, {"node": settings.node_name, "outcome": "refused"})
        logger.warning("collective cleanup refused", extra=fields | {"outcome": "refused"})
        raise
    cleanup_outcomes.add(1, {"node": settings.node_name, "outcome": "interrupted"})
    logger.info("collective cleanup retained", extra=fields | {"outcome": "interrupted"})
    return receipt
