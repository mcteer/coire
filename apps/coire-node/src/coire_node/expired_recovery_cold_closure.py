"""Exact local fence closure after verified Source ownership and supervision."""

from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeColdClosureSupervision,
    UpgradeExpiredRecoveryColdClosure,
    UpgradeExpiredRecoveryColdClosureIntent,
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdResolutionObservation,
    UpgradeExpiredRecoveryColdResolutionRead,
    UpgradeExpiredRecoveryColdResolutionRequest,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
    UpgradeRecoveryLocalFenceObservation,
)
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NativeGuardStatus,
    NativeNodeProcess,
    NodeMaintenanceCommand,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_handback import (
    source_writer_lock_held,
    verify_handback_boundary,
)
from coire_node.expired_recovery_cold_source import stores
from coire_node.expired_recovery_cold_supervision import verify_fixture_supervisor
from coire_node.maintenance import NodeMaintenance
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.cold_recovery_closure")


@tracer.start_as_current_span("coire.node.cold_recovery_close_source_fence")
def close_cold_source(
    settings: Settings,
    supervision: UpgradeExpiredRecoveryColdSupervisionReceipt,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdClosure:
    handback = supervision.supervision.handback
    owner = handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    bootstrap, run, _ = stores(settings, owner)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    require_recovery_lease(bootstrap, owner.sha256())
    with run.ownership("cold.closure.lock"):
        if (
            run.read("cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt)
            != supervision
        ):
            raise UpgradeConflict("Cold closure has no exact retained Source supervision")
        if control.accept_cold_recovery_handback_receipt(handback) != handback:
            raise UpgradeUnavailable("Cold closure has no live committed Source handback")
        verify_fixture_supervisor(settings, supervision)
        gate = NodeMaintenance(node.root, owner.original_claim.node)
        intent = run.read("cold.closure.intent.json", UpgradeExpiredRecoveryColdClosureIntent)
        if intent is None:
            verify_handback_boundary(settings, handback.helper.handback, control)
            before = gate.current()
            if before is None:
                raise UpgradeUnavailable("Cold closure original local fence is absent")
            intent = UpgradeExpiredRecoveryColdClosureIntent(
                supervision=supervision, before_maintenance=before, started_at=datetime.now(UTC)
            )
            run.write("cold.closure.intent.json", intent, replace=False)
        if intent.supervision != supervision:
            raise UpgradeConflict("Cold closure retry changes retained supervision")
        verify_handback_boundary(settings, handback.helper.handback, control, allow_released=True)
        fence = gate.current()
        if fence is None:
            raise UpgradeUnavailable("Cold closure lost original maintenance")
        if fence.state != "released":
            if fence != intent.before_maintenance:
                raise UpgradeConflict("Cold closure original fence preimage changed")
            inventory = control.local_inventory(
                owner.intent.local_inventory_plan(), deadline_at=owner.intent.deadline_at
            )
            if (
                inventory.runtime_sha256 != owner.original_native.source.runtime.environment_sha256
                or not inventory.idle(NODE_UPGRADE_OWNERS)
                or not 0 <= (datetime.now(UTC) - inventory.observed_at).total_seconds() <= 5
            ):
                raise UpgradeUnavailable("Cold closure Source still has actual work obligations")
            verify_fixture_supervisor(settings, supervision)
            if control.accept_cold_recovery_handback_receipt(handback) != handback:
                raise UpgradeUnavailable("Cold closure authority changed before local release")
        require_recovery_lease(bootstrap, owner.sha256())
        if not source_writer_lock_held(bootstrap):
            raise UpgradeUnavailable("Cold closure Source writer ownership is absent")
        # Source restoration also retires the original exact sparse staging
        # allocation before releasing its local recovery fence.
        from coire_node.upgrade_executor import NativeDiskReservation

        reservation = (
            Path(settings.upgrade_environment_root)
            / ".upgrade-reservations"
            / owner.intent.request.original_job_id
        )
        if reservation.exists() or reservation.is_symlink():
            if datetime.now(UTC) >= owner.intent.deadline_at:
                raise UpgradeUnavailable("Cold closure staging cleanup deadline elapsed")
            released_hold = NativeDiskReservation(reservation, owner.intent.original_plan).release()
            if released_hold is None or released_hold.state != "released":
                raise UpgradeUnavailable("Cold closure original staging ownership is unresolved")
        acknowledgement = gate.apply(
            NodeMaintenanceCommand(
                maintenance=intent.before_maintenance, action="release", terminal_verified=True
            )
        )
        verify_handback_boundary(settings, handback.helper.handback, control, allow_released=True)
        verify_fixture_supervisor(settings, supervision)
        status = bootstrap.read("status.json", NativeGuardStatus)
        process = node.read("node-process.json", NativeNodeProcess)
        if status is None or process is None:
            raise UpgradeUnavailable("Cold closure actual Source status or process is absent")
        maintenance = gate.current()
        if maintenance is None or maintenance.state != "released":
            raise UpgradeUnavailable("Cold closure cannot freshly observe its released fence")
        local_observation = UpgradeRecoveryLocalFenceObservation(
            maintenance=maintenance, observed_at=datetime.now(UTC)
        )
        receipt = UpgradeExpiredRecoveryColdClosure(
            handback=handback,
            supervision=UpgradeColdClosureSupervision.from_receipt(supervision),
            local_release=acknowledgement,
            status=status,
            node=process,
            local_observation=local_observation,
            observed_at=datetime.now(UTC),
        )
        previous = run.read("cold.closure.receipt.json", UpgradeExpiredRecoveryColdClosure)
        if previous is not None:
            if previous.handback != handback or previous.supervision not in (
                supervision,
                UpgradeColdClosureSupervision.from_receipt(supervision),
            ):
                raise UpgradeConflict("Cold closure retained history has another owner")
            return previous
        run.write("cold.closure.receipt.json", receipt, replace=False)
        return receipt


@tracer.start_as_current_span("coire.node.cold_recovery_resolve")
def resolve_closed_source(
    settings: Settings, closure: UpgradeExpiredRecoveryColdClosure, control: NativeGuardTransport
) -> UpgradeExpiredRecoveryColdResolution:
    owner = closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    bootstrap, run, _ = stores(settings, owner)
    require_recovery_lease(bootstrap, owner.sha256())
    if run.read("cold.closure.receipt.json", UpgradeExpiredRecoveryColdClosure) != closure:
        raise UpgradeConflict("Cold resolution has no exact retained local closure")
    with run.ownership("cold.resolution.lock"):
        state = control.cold_resolution_state(
            owner.intent.recovery_id,
            UpgradeExpiredRecoveryColdResolutionRead(
                intent_sha256=owner.intent.sha256(), closure_sha256=closure.sha256()
            ),
        )
        if state.resolution is not None:
            result = state.resolution
        else:
            verify_handback_boundary(
                settings, closure.handback.helper.handback, control, allow_released=True
            )
            supervision = run.read(
                "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
            )
            if supervision is None or closure.supervision not in (
                supervision,
                UpgradeColdClosureSupervision.from_receipt(supervision),
            ):
                raise UpgradeConflict(
                    "Cold closure supervision differs from retained exact receipt"
                )
            verify_fixture_supervisor(settings, supervision)
            if not source_writer_lock_held(bootstrap):
                raise UpgradeUnavailable("Cold resolution Source writer ownership is absent")
            node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
            status = bootstrap.read("status.json", NativeGuardStatus)
            process = node.read("node-process.json", NativeNodeProcess)
            maintenance = node.read("maintenance.json", UpgradeMaintenance)
            if status is None or process is None or maintenance is None:
                raise UpgradeUnavailable("Cold resolution current physical Source facts are absent")
            observation = UpgradeExpiredRecoveryColdResolutionObservation(
                closure_sha256=closure.sha256(),
                status=status,
                node=process,
                maintenance=maintenance,
                observed_at=datetime.now(UTC),
            )
            body = UpgradeExpiredRecoveryColdResolutionRequest(
                closure=closure, observation=observation
            )
            # This is a fresh passive observation, not a replacement effect
            # receipt or a recaptured grant. Old intent/deadlines stay fixed.
            run.write("cold.resolution.observation.json", observation)
            result = control.resolve_cold_recovery(body)
        if result.closure != closure:
            raise UpgradeConflict("Cold resolution acknowledgment changes local closure")
        previous = run.read("cold.resolution.json", UpgradeExpiredRecoveryColdResolution)
        if previous is not None and previous != result:
            raise UpgradeConflict("Cold resolution retry changes committed history")
        if previous is None:
            run.write("cold.resolution.json", result, replace=False)
        return previous or result
