"""Persist exact Core verification without recapturing data or executable authority."""

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdPairQualification,
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdVerification,
    UpgradeExpiredRecoveryColdVerificationRequest,
)
from coire_core.settings import Settings
from coire_node.expired_recovery_cold_owner import verify_cold_physical
from coire_node.expired_recovery_cold_source import stores, verify_projection_boundary
from coire_node.expired_recovery_native_data import capture_native_data
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.cold_recovery_verification")


@tracer.start_as_current_span("coire.node.cold_recovery_verify_source")
def verify_cold_source(
    settings: Settings,
    smoke: UpgradeExpiredRecoveryColdSmokeReceipt,
    pair: UpgradeExpiredRecoveryColdPairQualification | None,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdVerification:
    effect = smoke.smoke.source.launch.source.projection
    owner = effect.owner
    bootstrap, run, _ = stores(settings, owner)
    with run.ownership("cold.verification.lock"):
        require_recovery_lease(bootstrap, owner.sha256())
        previous = run.read("cold.verification.json", UpgradeExpiredRecoveryColdVerification)
        if previous is not None:
            if previous.request.smoke != smoke or previous.request.pair != pair:
                raise UpgradeConflict("Cold verification changes retained serving/pair preimages")
            if control.verify_cold_recovery_verification(previous) != previous:
                raise UpgradeUnavailable(
                    "Cold verification historical metadata has no live authority"
                )
            verify_cold_physical(settings, owner, effect)
            return previous
        request = run.read(
            "cold.verification.request.json", UpgradeExpiredRecoveryColdVerificationRequest
        )
        if request is None:
            verify_projection_boundary(settings, effect, control)
            after = capture_native_data(
                settings, owner.intent.original_plan, deadline_at=owner.intent.deadline_at
            )
            request = UpgradeExpiredRecoveryColdVerificationRequest(
                smoke=smoke,
                pair=pair,
                native_after=after,
                archive=run.read(
                    "cold.collective.archive.receipt.json",
                    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
                ),
            )
            verify_projection_boundary(settings, effect, control)
            run.write("cold.verification.request.json", request, replace=False)
        if request.smoke != smoke or request.pair != pair:
            raise UpgradeConflict("Cold verification retry changes captured request")
        # Replay this exact request after a lost commit. Core's already accepted
        # proof permits only handback; no ordinary owner fallback is attempted.
        proof = control.prepare_cold_recovery_verification(request)
        if proof.request != request or control.verify_cold_recovery_verification(proof) != proof:
            raise UpgradeUnavailable("Cold verification acknowledgment/live authority differs")
        verify_cold_physical(settings, owner, effect)
        require_recovery_lease(bootstrap, owner.sha256())
        run.write("cold.verification.json", proof, replace=False)
        return proof
