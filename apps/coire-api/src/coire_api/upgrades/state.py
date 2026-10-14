"""Monotonic upgrade progress with evidence gates for terminal reconciliation."""

from collections.abc import Sequence

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMigrationNotStarted,
    UpgradeOperation,
    UpgradeReceipt,
)
from coire_core.models.upgrades import (
    UpgradePhase as Phase,
)

_FORWARD = {
    Phase.ACCEPTED: {Phase.PREFLIGHT},
    Phase.PREFLIGHT: {Phase.FENCING, Phase.STAGED},
    Phase.FENCING: {Phase.DRAINING},
    Phase.DRAINING: {Phase.STAGED},
    Phase.STAGED: {Phase.FENCING, Phase.SMOKE_BEFORE, Phase.MIGRATING, Phase.ACTIVATING},
    Phase.SMOKE_BEFORE: {Phase.ACTIVATING},
    Phase.MIGRATING: {Phase.ACTIVATING},
    Phase.ACTIVATING: {Phase.VERIFYING},
    Phase.VERIFYING: {Phase.QUALIFYING, Phase.COMMITTING},
    Phase.QUALIFYING: {Phase.COMMITTING},
    Phase.COMMITTING: {Phase.SUCCEEDED},
    Phase.RESTORING: {Phase.VERIFYING_SOURCE},
    Phase.VERIFYING_SOURCE: {Phase.ROLLED_BACK},
    Phase.RECOVERY_REQUIRED: {Phase.RESTORING, Phase.VERIFYING_SOURCE},
}


def validate_progress(
    operation: UpgradeOperation,
    receipt: UpgradeReceipt,
    plan: UpgradeExecutionPlan,
    history: Sequence[UpgradeReceipt],
    *,
    activation_authorized: bool = False,
    migration_not_started: UpgradeMigrationNotStarted | None = None,
) -> None:
    """No terminal label, health status, or elapsed deadline substitutes for proof."""
    if receipt.effect_id.startswith("expired-recovery-"):
        raise UpgradeConflict("Ordinary progress cannot resolve a distinct expired recovery intent")
    if (
        (receipt.job_id, receipt.generation) != (operation.job_id, operation.generation)
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or operation.phase in TERMINAL_UPGRADE_PHASES
    ):
        raise UpgradeConflict("Progress owner is stale or terminal")
    activated = any(item.phase is Phase.ACTIVATING and item.outcome == "intent" for item in history)
    migrated = any(item.phase is Phase.MIGRATING and item.outcome == "intent" for item in history)
    destination = receipt.phase
    if (
        destination in {Phase.ACTIVATING, Phase.MIGRATING}
        and receipt.outcome == "intent"
        and not activation_authorized
    ):
        raise UpgradeConflict("External activation intent requires consumed live authority")
    if (
        destination in {Phase.ACTIVATING, Phase.MIGRATING}
        and receipt.outcome != "intent"
        and not any(item.phase is destination and item.outcome == "intent" for item in history)
    ):
        raise UpgradeConflict("Activation completion has no consumed intent")
    if (
        destination in {Phase.VERIFYING, Phase.QUALIFYING, Phase.COMMITTING, Phase.SUCCEEDED}
        and not activated
    ):
        raise UpgradeConflict("Candidate progress has no consumed runtime activation")
    permitted = _FORWARD.get(operation.phase, set()) | {operation.phase, Phase.RECOVERY_REQUIRED}
    if activated:
        permitted |= {Phase.RESTORING}
    else:
        permitted |= {Phase.FAILED_UNCHANGED, Phase.CANCELLED_UNCHANGED}
    if destination not in permitted:
        raise UpgradeConflict("Progress transition differs from the recovery protocol")
    selection_decision = (
        operation.kind is UpgradeKind.CONTROL_PLANE
        and destination is Phase.COMMITTING
        and receipt.outcome == "complete"
    )
    if destination not in TERMINAL_UPGRADE_PHASES and not selection_decision:
        return
    if receipt.outcome != "complete" or not receipt.evidence:
        raise UpgradeConflict("Terminal progress requires completed verification")
    evidence = UpgradeEvidence.model_validate_json(receipt.evidence)
    if evidence.cleanup_verified is not True:
        raise UpgradeConflict("Terminal progress requires owned cleanup verification")
    success = destination is Phase.SUCCEEDED or selection_decision
    if (
        operation.kind is UpgradeKind.CONTROL_PLANE
        and destination is Phase.SUCCEEDED
        and not any(
            item.phase is Phase.COMMITTING and item.outcome == "complete" for item in history
        )
    ):
        raise UpgradeConflict("Core success requires an acknowledged selection decision")
    if success and operation.cancel_requested:
        raise UpgradeConflict("Cancelled activation requires restoration before termination")
    expected = plan.target if success else plan.source
    if receipt.observed_sha256 != expected.manifest_sha256:
        raise UpgradeConflict("Terminal observed release differs")
    if not success and evidence.source_verified is not True:
        raise UpgradeConflict("Recovery requires positive source verification")
    if operation.kind is UpgradeKind.STUDIO:
        if evidence.runtime_sha256 != expected.manifest.environment_sha256:
            raise UpgradeConflict("Terminal runtime differs from the selected environment")
    elif evidence.service_image_ids != {
        service.service: service.image_id for service in expected.manifest.services
    }:
        raise UpgradeConflict("Terminal Core cohort differs from the selected images")
    if operation.kind is UpgradeKind.CONTROL_PLANE and (
        evidence.schema_head is None
        or evidence.schema_head not in expected.manifest.compatibility.readable_schema_heads
        or (success and evidence.schema_head != plan.schema_head_after)
    ):
        raise UpgradeConflict("Terminal Core schema is unknown or incompatible")
    if destination in {Phase.FAILED_UNCHANGED, Phase.CANCELLED_UNCHANGED}:
        if activated:
            raise UpgradeConflict("An activated runtime requires restoration")
        if migrated:
            completed = (
                evidence.migration_exit_code == 0 and evidence.schema_head == plan.schema_head_after
            )
            unchanged = (
                evidence.migration_exit_code is not None
                and evidence.migration_exit_code != 0
                and evidence.schema_head == plan.initial_schema_head
                and evidence.schema_before_sha256 == plan.initial_schema_sha256
                and evidence.schema_after_sha256 == plan.initial_schema_sha256
            )
            not_started = (
                migration_not_started is not None
                and (
                    migration_not_started.job_id,
                    migration_not_started.generation,
                    migration_not_started.plan_sha256,
                    migration_not_started.schema_head,
                    migration_not_started.schema_sha256,
                )
                == (
                    operation.job_id,
                    operation.generation,
                    plan.authority_sha256(),
                    plan.initial_schema_head,
                    plan.initial_schema_sha256,
                )
                and evidence.migration_not_started_sha256 == migration_not_started.sha256()
                and evidence.migration_exit_code is None
                and evidence.schema_head == plan.initial_schema_head
                and evidence.schema_before_sha256 == plan.initial_schema_sha256
                and evidence.schema_after_sha256 == plan.initial_schema_sha256
            )
            if (
                not (completed or unchanged or not_started)
                or evidence.schema_head
                not in plan.source.manifest.compatibility.readable_schema_heads
            ):
                raise UpgradeConflict("Uncertain or incompatible schema retains recovery ownership")
        return
    verification_phase = Phase.VERIFYING if success else Phase.VERIFYING_SOURCE
    verification = next(
        (
            item
            for item in reversed(history)
            if (
                item.phase is verification_phase
                and item.outcome == "complete"
                and item.observed_sha256 == expected.manifest_sha256
                and item.evidence
            )
        ),
        None,
    )
    if verification is None:
        raise UpgradeConflict("Terminal progress requires durable post-activation verification")
    assert verification.evidence is not None
    prior = UpgradeEvidence.model_validate_json(verification.evidence)
    binding_fields = (
        "model_id",
        "model_variant_id",
        "model_manifest_sha256",
        "output_sha256",
        "generated_tokens",
        "runtime_sha256",
        "service_image_ids",
        "schema_head",
        "check",
        "collective_verified",
        "peer_runtime_sha256",
    )
    if any(getattr(prior, field) != getattr(evidence, field) for field in binding_fields):
        raise UpgradeConflict("Terminal serving proof differs from its durable verification")
    if (
        evidence.generated_tokens is None
        or evidence.generated_tokens < 10
        or evidence.output_sha256 is None
        or evidence.model_id != plan.model_id
        or evidence.model_variant_id != plan.model_variant_id
        or evidence.model_manifest_sha256 != plan.model_manifest_sha256
    ):
        raise UpgradeConflict("Terminal verification requires exact fresh serving evidence")
    if (
        operation.kind is UpgradeKind.STUDIO
        and success
        and (plan.source.manifest.paired_required or plan.target.manifest.paired_required)
        and (
            evidence.collective_verified is not True
            or evidence.peer_runtime_sha256 != plan.peer_runtime_sha256
        )
    ):
        raise UpgradeConflict("Success requires exact peer collective qualification")
