"""Distinct recovery collective dispatches, never replaying original probes."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.sharding import LinkProbeCommand, ProbeTransport
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdPairCommand,
    UpgradeExpiredRecoveryColdPairQualification,
    UpgradeExpiredRecoveryColdPairResult,
    UpgradeExpiredRecoveryColdSmokeReceipt,
)
from coire_core.models.upgrades import NODE_UPGRADE_OWNERS, NodeUpgradeInventory, UpgradeMaintenance
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_archive import history_sha256
from coire_node.expired_recovery_cold_launch import require_live_cold_source
from coire_node.expired_recovery_cold_source import stores, verify_projection_boundary
from coire_node.expired_recovery_definition import read_definition_file
from coire_node.link_probes import LinkProbeRunner
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.cold_recovery")


def verify_pair_inventory(
    smoke: UpgradeExpiredRecoveryColdSmokeReceipt,
    local: NodeUpgradeInventory,
    peer: NodeUpgradeInventory,
) -> None:
    claim = smoke.smoke.source.launch.source.projection.owner
    now = datetime.now(UTC)
    for snapshot, node, runtime in (
        (local, "coire-edge-a", claim.original_native.source.runtime.environment_sha256),
        (peer, "coire-edge-b", claim.intent.original_plan.peer_runtime_sha256),
    ):
        if (
            (snapshot.job_id, snapshot.generation, snapshot.node, snapshot.runtime_sha256)
            != (
                (
                    claim.intent.local_maintenance().job_id
                    if node == claim.original_claim.node
                    else claim.original_claim.job_id
                ),
                (
                    claim.intent.local_maintenance().generation
                    if node == claim.original_claim.node
                    else claim.original_claim.generation
                ),
                node,
                runtime,
            )
            or not 0 <= (now - snapshot.observed_at).total_seconds() <= 5
            or not snapshot.idle(NODE_UPGRADE_OWNERS)
        ):
            raise UpgradeUnavailable(
                "Cold exact-pair dispatch retains stale, busy or foreign inventory"
            )


@tracer.start_as_current_span("coire.node.cold_recovery_qualify_pair")
def qualify_cold_pair(
    settings: Settings,
    smoke: UpgradeExpiredRecoveryColdSmokeReceipt,
    archive: UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    control: NativeGuardTransport,
    runner: LinkProbeRunner,
) -> UpgradeExpiredRecoveryColdPairQualification:
    source = smoke.smoke.source
    effect = source.launch.source.projection
    claim = effect.owner
    if settings.node_name != "coire-edge-a":
        raise UpgradeUnavailable("Cold pair dispatch requires declared rank zero")
    bootstrap, run, _ = stores(settings, claim)
    original = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)

    def authority() -> None:
        require_recovery_lease(bootstrap, claim.sha256())
        if control.verify_cold_recovery_smoke(smoke.smoke) != smoke.smoke:
            raise UpgradeUnavailable("Cold pair live Core registry authority differs")
        fence = original.read("maintenance.json", UpgradeMaintenance)
        if fence is None or not fence.matches_plan(claim.intent.local_inventory_plan()):
            raise UpgradeConflict("Cold pair original maintenance changed")

    def boundary() -> None:
        verify_projection_boundary(settings, effect, control)
        require_live_cold_source(settings, source)
        authority()
        if (
            run.read("cold.smoke.receipt.json", UpgradeExpiredRecoveryColdSmokeReceipt) != smoke
            or run.read(
                "cold.collective.archive.receipt.json",
                UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
            )
            != archive
        ):
            raise UpgradeConflict("Cold pair has no exact retained serving and cleanup provenance")
        path = run.root / "collective-history" / claim.original_claim.job_id
        if archive.archive.scopes:
            archived = PrivateUpgradeStore(path, create=False)
            if history_sha256(archived) != archive.archive.history_sha256:
                raise UpgradeConflict("Cold pair original dispatch archive changed")
        else:
            import hashlib

            original = (
                Path(settings.node_state_dir) / "upgrades/collectives" / claim.original_claim.job_id
            )
            if (
                claim.intent.request.original_scope_sha256s
                or original.exists()
                or original.is_symlink()
                or path.exists()
                or path.is_symlink()
                or archive.archive.history_sha256
                != hashlib.sha256(b"coire-cold-no-original-dispatch-v1").hexdigest()
            ):
                raise UpgradeConflict("Cold pair absent original dispatch history changed")

    def inventories() -> tuple[NodeUpgradeInventory, NodeUpgradeInventory]:
        plan = claim.intent.original_plan
        local = control.local_inventory(
            claim.intent.local_inventory_plan(), deadline_at=claim.intent.deadline_at
        )
        peer = control.peer_inventory(plan, deadline_at=claim.intent.deadline_at)
        verify_pair_inventory(smoke, local, peer)
        return local, peer

    with run.ownership("cold.pair.lock"):
        boundary()
        retained = run.read(
            "cold.pair.qualification.json", UpgradeExpiredRecoveryColdPairQualification
        )
        if retained is not None:
            if (
                retained.results[0].dispatch.smoke != smoke
                or retained.results[0].dispatch.archive != archive
            ):
                raise UpgradeConflict("Cold pair qualification belongs to another recovery scope")
            inventories()  # Historical qualification never stands in for current cleanup.
            return retained
        hostfile = read_definition_file(Path(settings.sharding_jaccl_hostfile), os.getuid())
        hostfile_sha256 = hashlib.sha256(hostfile).hexdigest()
        results = []
        for index in range(3):
            boundary()
            dispatch = run.read(
                f"cold.pair.{index}.command.json", UpgradeExpiredRecoveryColdPairCommand
            )
            result = run.read(
                f"cold.pair.{index}.result.json", UpgradeExpiredRecoveryColdPairResult
            )
            if dispatch is not None:
                if (
                    dispatch.smoke != smoke
                    or dispatch.archive != archive
                    or dispatch.index != index
                    or dispatch.command.hostfile_sha256 != hostfile_sha256
                ):
                    raise UpgradeConflict(
                        "Cold pair dispatch differs from retained exact authority"
                    )
                if result is None:
                    raise UpgradeUnavailable(
                        "Cold pair interrupted; cleanup-only reconciliation is required"
                    )
                if result.dispatch != dispatch:
                    raise UpgradeConflict("Cold pair result has another dispatch preimage")
            else:
                if result is not None:
                    raise UpgradeConflict("Cold pair result has no retained dispatch")
                inventories()
                dispatch = UpgradeExpiredRecoveryColdPairCommand(
                    smoke=smoke,
                    archive=archive,
                    index=index,
                    command=LinkProbeCommand(
                        command_id=uuid5(
                            NAMESPACE_URL,
                            f"coire-expired-recovery-pair/{claim.intent.recovery_id}/{claim.intent.sha256()}/{index}",
                        ),
                        transport=ProbeTransport.JACCL,
                        hostfile_sha256=hostfile_sha256,
                    ),
                    captured_at=datetime.now(UTC),
                )
                authority()
                run.write(f"cold.pair.{index}.command.json", dispatch, replace=False)
                observation = runner.run(
                    dispatch.command, deadline_at=claim.intent.deadline_at, authority=authority
                )
                result = UpgradeExpiredRecoveryColdPairResult(
                    dispatch=dispatch, observation=observation
                )
                # Keep actual failed/foreign-runtime outcomes before any final
                # authority check can fail. A retry cannot dispatch this again.
                run.write(f"cold.pair.{index}.result.json", result, replace=False)
            if not result.qualified():
                raise UpgradeUnavailable(
                    "Cold pair actual worker result does not qualify the exact Source pair"
                )
            boundary()
            results.append(result)
        local, peer = inventories()
        authority()
        qualification = UpgradeExpiredRecoveryColdPairQualification(
            results=tuple(results),
            local=local,
            peer=peer,
            observed_at=datetime.now(UTC),
        )
        run.write("cold.pair.qualification.json", qualification, replace=False)
        return qualification
