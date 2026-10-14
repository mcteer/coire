"""Producer-owned Source selection projection under separate cold authority."""

from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdSourceIntent,
    UpgradeExpiredRecoveryColdSourceReceipt,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    PreparedNativeRuntime,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_owner import verify_cold_physical
from coire_node.expired_recovery_cold_telemetry import record_cold_effect
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport
from coire_node.upgrade_runtime import reconcile_native_selection

tracer = trace.get_tracer("coire.node.cold_recovery")


def stores(
    settings: Settings, claim: UpgradeExpiredRecoveryColdClaim
) -> tuple[PrivateUpgradeStore, PrivateUpgradeStore, PrivateUpgradeStore]:
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(claim.intent.recovery_id), create=False)
    operations = PrivateUpgradeStore(bootstrap.path("operations"), create=False)
    operation = PrivateUpgradeStore(
        operations.path(claim.intent.request.original_job_id), create=False
    )
    guard = PrivateUpgradeStore(operation.path("guard"), create=False)
    return bootstrap, run, guard


def verify_projection_boundary(
    settings: Settings,
    effect: UpgradeExpiredRecoveryColdSourceIntent,
    control: NativeGuardTransport,
) -> None:
    try:
        _verify_projection_boundary(settings, effect, control)
    except CoireError:
        record_cold_effect(effect.owner, "refused")
        raise


def _verify_projection_boundary(
    settings: Settings,
    effect: UpgradeExpiredRecoveryColdSourceIntent,
    control: NativeGuardTransport,
) -> None:
    claim = effect.owner
    bootstrap, run, guard = stores(settings, claim)
    require_recovery_lease(bootstrap, claim.sha256())
    if (
        run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) != claim
        or run.read("cold.activation.json", UpgradeExpiredRecoveryColdClaim) != claim
    ):
        raise UpgradeConflict("Cold projection has no exact activated owner")
    if control.verify_cold_recovery_owner(claim) != claim:
        raise UpgradeUnavailable("Cold projection Core authority differs")
    verify_cold_physical(settings, claim, effect)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    fence = node.read("maintenance.json", UpgradeMaintenance)
    late = claim.continuation.post_handback if claim.continuation is not None else None
    valid_released = late is not None and fence == late.released_maintenance
    if fence is None or (
        not valid_released and not fence.matches_plan(claim.intent.local_inventory_plan())
    ):
        raise UpgradeConflict("Cold Source maintenance authority differs")
    if bootstrap.read("runtime.json", PreparedNativeRuntime) not in (
        effect.before_bootstrap_runtime,
        claim.runtime,
    ):
        raise UpgradeConflict("Cold bootstrap runtime has an unknown preimage")
    native = guard.read("intent.json", NativeRecoveryIntent)
    envs = Path(settings.upgrade_environment_root)
    current = envs / "current"
    if not current.is_symlink() or current.resolve(strict=True) not in {
        (envs / effect.before_pointer.environment_id).resolve(strict=True),
        (envs / effect.source_selection.runtime.environment_id).resolve(strict=True),
    }:
        raise UpgradeConflict("Cold Source pointer has an unknown preimage")
    if native == effect.projected_native and current.resolve(strict=True) != (
        envs / effect.source_selection.runtime.environment_id
    ).resolve(strict=True):
        raise UpgradeConflict("Cold native projection precedes physical Source selection")
    require_recovery_lease(bootstrap, claim.sha256())


@tracer.start_as_current_span("coire.node.cold_recovery_select_source")
def project_cold_source(
    settings: Settings, claim: UpgradeExpiredRecoveryColdClaim, control: NativeGuardTransport
) -> UpgradeExpiredRecoveryColdSourceReceipt:
    """Reconcile exact recorded before/after states, never renew or qualify old work."""
    bootstrap, run, guard = stores(settings, claim)
    require_recovery_lease(bootstrap, claim.sha256())
    with run.ownership("cold.source.lock"):
        effect = run.read("cold.source.intent.json", UpgradeExpiredRecoveryColdSourceIntent)
        pointer = PrivateUpgradeStore(guard.root.parent / "pointer")
        if effect is None:
            if control.verify_cold_recovery_owner(claim) != claim:
                raise UpgradeUnavailable("Cold Source has no exact live owner")
            verify_cold_physical(settings, claim)
            envs = Path(settings.upgrade_environment_root)
            current = envs / "current"
            if not current.is_symlink():
                raise UpgradeUnavailable("Cold current pointer cannot be observed")
            selected_path = current.resolve(strict=True)
            known = [
                r
                for r in (claim.original_native.source.runtime, claim.original_native.candidate)
                if selected_path == (envs / r.environment_id).resolve(strict=True)
            ]
            if not known:
                raise UpgradeConflict("Cold current pointer is outside retained authority")
            runtime = bootstrap.read("runtime.json", PreparedNativeRuntime)
            if runtime is None:
                raise UpgradeUnavailable("Cold bootstrap runtime preimage is absent")
            now = datetime.now(UTC)
            op = claim.intent.original_plan.operation
            choice = (
                claim.original_native.source
                if claim.original_native.activation_started_at is None
                else NativeRuntimeSelection(
                    node=claim.previous_guard.node,
                    runtime=claim.original_native.source.runtime,
                    release_sha256=claim.original_native.source.release_sha256,
                    job_id=op.job_id,
                    generation=op.generation,
                    selected_at=now,
                )
            )
            if claim.continuation is not None:
                choice = claim.continuation.retained_node.selected
            projected = NativeRecoveryIntent.model_validate(
                claim.original_native.model_dump(mode="json")
                | {
                    "phase": "prepared"
                    if claim.original_native.activation_started_at is None
                    else "source_selected",
                    "updated_at": now,
                }
            )
            effect = UpgradeExpiredRecoveryColdSourceIntent(
                owner=claim,
                before_pointer=known[0],
                before_pointer_selection=pointer.read("selection.json", NativeRuntimeSelection),
                before_bootstrap_runtime=runtime,
                source_selection=choice,
                projected_native=projected,
                prepared_at=now,
            )
            verify_projection_boundary(settings, effect, control)
            run.write("cold.source.intent.json", effect, replace=False)
        if effect.owner != claim:
            raise UpgradeConflict("Cold Source projection belongs to another owner")
        verify_projection_boundary(settings, effect, control)
        selected = pointer.read("selection.json", NativeRuntimeSelection)
        if selected not in (effect.before_pointer_selection, effect.source_selection):
            raise UpgradeConflict("Cold pointer metadata changed outside recorded transition")
        if selected != effect.source_selection:
            verify_projection_boundary(settings, effect, control)
            pointer.write("selection.json", effect.source_selection)
        reconcile_native_selection(
            Path(settings.upgrade_environment_root),
            Path(settings.upgrade_environment_root).parent / "runtimes",
            pointer.root,
            effect.source_selection.node,
            before_replace=lambda: verify_projection_boundary(settings, effect, control),
        )
        verify_projection_boundary(settings, effect, control)
        if guard.read("intent.json", NativeRecoveryIntent) != effect.projected_native:
            guard.write("intent.json", effect.projected_native)
        verify_projection_boundary(settings, effect, control)
        if bootstrap.read("runtime.json", PreparedNativeRuntime) != claim.runtime:
            bootstrap.write("runtime.json", claim.runtime)
        verify_projection_boundary(settings, effect, control)
        previous = run.read("cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt)
        if previous is not None:
            if previous.projection != effect:
                raise UpgradeConflict("Cold Source receipt history differs")
            return previous
        receipt = UpgradeExpiredRecoveryColdSourceReceipt(
            projection=effect, observed_at=datetime.now(UTC)
        )
        run.write("cold.source.receipt.json", receipt, replace=False)
        record_cold_effect(claim, "source_selected")
        return receipt


def publish_cold_source_readiness(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryColdSourceReceipt,
    control: NativeGuardTransport,
) -> NativeGuardStatus:
    """Recovery-owner readiness behind maintenance, never serving qualification."""
    effect = receipt.projection
    verify_projection_boundary(settings, effect, control)
    bootstrap, run, guard = stores(settings, effect.owner)
    if (
        run.read("cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt) != receipt
        or guard.read("intent.json", NativeRecoveryIntent) != effect.projected_native
    ):
        raise UpgradeConflict("Cold recovery readiness has no committed Source projection")
    if bootstrap.read("runtime.json", PreparedNativeRuntime) != effect.owner.runtime:
        raise UpgradeConflict("Cold recovery bootstrap runtime is not committed")
    status = NativeGuardStatus(
        node=effect.source_selection.node,
        bootstrap_runtime=effect.owner.runtime,
        process=effect.owner.process,
        selected=effect.source_selection,
        ready=True,
        observed_at=datetime.now(UTC),
    )
    require_recovery_lease(bootstrap, effect.owner.sha256())
    bootstrap.write("status.json", status)
    record_cold_effect(effect.owner, "source_guard_ready")
    return status
