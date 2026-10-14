"""Fresh sealed-Source generation, isolated from the original smoke journals."""

from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrades import NativeSmokeAttempt, NativeSmokeCommand, UpgradeMaintenance
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_launch import require_live_cold_source
from coire_node.expired_recovery_cold_source import stores, verify_projection_boundary
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_child import run_guard_smoke
from coire_node.upgrade_guard import NativeGuardJournal, NativeGuardTransport

tracer = trace.get_tracer("coire.node.cold_recovery")


@tracer.start_as_current_span("coire.node.cold_recovery_source_smoke")
def run_cold_source_smoke(
    settings: Settings,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdSmokeReceipt:
    """New real generation under captured budget; no original evidence is replaced.

    The unchanged sealed Source's existing smoke module owns its bare engine.
    The current immutable support parent owns admission, pipes and its distinct
    private namespace. Live Core/registry revocation is polled during exchange;
    uncertainty retains all original maintenance and local cleanup obligations.
    """
    effect = source.launch.source.projection
    claim = effect.owner
    bootstrap, run, _ = stores(settings, claim)
    with run.ownership("cold.smoke.lock"):
        verify_projection_boundary(settings, effect, control)
        require_live_cold_source(settings, source)
        smoke = run.read("cold.smoke.command.json", UpgradeExpiredRecoveryColdSmokeCommand)
        if smoke is None:
            smoke = UpgradeExpiredRecoveryColdSmokeCommand(
                source=source,
                command=NativeSmokeCommand(
                    plan=claim.intent.original_plan,
                    stage="source-serving",
                    runtime_sha256=effect.source_selection.runtime.environment_sha256,
                    deadline_at=claim.intent.deadline_at,
                ),
                captured_at=datetime.now(UTC),
            )
            if control.verify_cold_recovery_smoke(smoke) != smoke:
                raise UpgradeUnavailable("Cold Source smoke Core scope differs")
            run.write("cold.smoke.command.json", smoke, replace=False)
        if smoke.source != source:
            raise UpgradeConflict("Cold Source smoke retained process differs")
        if control.verify_cold_recovery_smoke(smoke) != smoke:
            raise UpgradeUnavailable("Cold Source smoke live registry scope differs")
        previous = run.read("cold.smoke.receipt.json", UpgradeExpiredRecoveryColdSmokeReceipt)
        if previous is not None:
            if previous.smoke != smoke:
                raise UpgradeConflict("Cold Source smoke receipt changes original evidence")
            return previous
        state = PrivateUpgradeStore(run.path("smoke-state"))
        fence_store = PrivateUpgradeStore(state.path("upgrades"))
        original = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
        fence = original.read("maintenance.json", UpgradeMaintenance)
        late = claim.continuation.post_handback if claim.continuation is not None else None
        if fence is None or (
            not fence.matches_plan(claim.intent.local_inventory_plan())
            and not (late is not None and fence == late.released_maintenance)
        ):
            raise UpgradeConflict("Cold Source smoke has no current original fence")
        retained_fence = fence_store.read("maintenance.json", UpgradeMaintenance)
        if retained_fence is None:
            fence_store.write(
                "maintenance.json",
                claim.intent.original_plan.maintenance.model_copy(update={"state": "active"})
                if late is not None or claim.intent.inherited_local_maintenance is not None
                else fence,
                replace=False,
            )
        elif not retained_fence.matches_plan(claim.intent.original_plan):
            raise UpgradeUnavailable("Cold Source smoke was revoked; no implicit retry")
        journal = NativeGuardJournal(
            run.path("smoke-operation"),
            claim.intent.original_plan,
            claim.original_native.source,
            claim.original_native.candidate,
            source.process,
        )
        shadow = settings.model_copy(update={"node_state_dir": str(state.root)})

        def authority() -> None:
            require_recovery_lease(bootstrap, claim.sha256())
            if control.verify_cold_recovery_smoke(smoke) != smoke:
                raise UpgradeUnavailable("Cold Source smoke live authorization differs")
            current = original.read("maintenance.json", UpgradeMaintenance)
            if current is None or (
                not current.matches_plan(claim.intent.local_inventory_plan())
                and not (late is not None and current == late.released_maintenance)
            ):
                raise UpgradeConflict("Cold Source smoke original maintenance changed")

        try:
            evidence = run_guard_smoke(shadow, journal, smoke.command, authority=authority)
        except CoireError:
            # This is only the recovery probe's private admission copy. Release
            # revokes the sealed child's next check; the real fence is retained.
            # No user process is signalled or resource obligation cleared here.
            fence_store.write(
                "maintenance.json",
                fence.model_copy(
                    update={
                        "state": "released",
                        "updated_at": datetime.now(UTC),
                    }
                ),
            )
            raise
        verify_projection_boundary(settings, effect, control)
        require_live_cold_source(settings, source)
        attempt = journal.store.read("source-serving.json", NativeSmokeAttempt)
        if attempt is None or attempt.evidence != evidence:
            raise UpgradeUnavailable("Cold Source smoke has no actual durable child outcome")
        receipt = UpgradeExpiredRecoveryColdSmokeReceipt(
            smoke=smoke, attempt=attempt, observed_at=datetime.now(UTC)
        )
        run.write("cold.smoke.receipt.json", receipt, replace=False)
        return receipt
