"""False terminal labels and missing activation authority retain recovery ownership."""

from datetime import UTC, datetime

import pytest
from upgrade_fixtures import plan

from coire_api.upgrades.state import validate_progress
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradeEvidence, UpgradePhase, UpgradeReceipt


def test_ordinary_progress_cannot_claim_after_deadline_resolution() -> None:
    value = plan()
    receipt = UpgradeReceipt(
        job_id=value.operation.job_id,
        generation=value.operation.generation,
        sequence=1,
        effect_id="expired-recovery-01ARZ3NDEKTSV4RRFFQ69G5FAW",
        phase=UpgradePhase.ROLLED_BACK,
        occurred_at=datetime.now(UTC),
        outcome="complete",
    )
    with pytest.raises(UpgradeConflict, match="distinct expired recovery intent"):
        validate_progress(value.operation, receipt, value, ())


@pytest.mark.parametrize("fault", [None, "missing", "digest", "plan", "exit"])
def test_unstarted_migration_terminal_requires_authoritative_fence(fault) -> None:
    from uuid import uuid4

    from upgrade_fixtures import core_plan

    from coire_core.models.upgrades import UpgradeMigrationNotStarted

    value = core_plan(schema_changed=True)
    operation = value.operation.model_copy(update={"phase": UpgradePhase.MIGRATING})
    proof = UpgradeMigrationNotStarted(
        job_id=operation.job_id,
        generation=operation.generation,
        plan_sha256="f" * 64 if fault == "plan" else value.authority_sha256(),
        grant_id=uuid4(),
        schema_head=value.initial_schema_head,
        schema_sha256=value.initial_schema_sha256,
        observed_at=datetime.now(UTC),
    )
    evidence = UpgradeEvidence(
        cleanup_verified=True,
        source_verified=True,
        schema_head=value.initial_schema_head,
        schema_before_sha256=value.initial_schema_sha256,
        schema_after_sha256=value.initial_schema_sha256,
        service_image_ids={item.service: item.image_id for item in value.source.manifest.services},
        migration_not_started_sha256="e" * 64 if fault == "digest" else proof.sha256(),
        migration_exit_code=0 if fault == "exit" else None,
    )
    intent = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="migration-activation",
        phase="migrating",
        outcome="intent",
        occurred_at=datetime.now(UTC),
        observed_sha256=operation.source_sha256,
    )
    terminal = intent.model_copy(
        update={
            "sequence": 2,
            "phase": UpgradePhase.FAILED_UNCHANGED,
            "outcome": "complete",
            "evidence": evidence.model_dump_json(),
        }
    )
    if fault:
        with pytest.raises(UpgradeConflict):
            validate_progress(
                operation,
                terminal,
                value,
                (intent,),
                migration_not_started=None if fault == "missing" else proof,
            )
    else:
        validate_progress(operation, terminal, value, (intent,), migration_not_started=proof)


def test_activation_intent_cannot_bypass_live_authority() -> None:
    value = plan()
    operation = value.operation.model_copy(update={"phase": UpgradePhase.SMOKE_BEFORE})
    intent = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="activation",
        phase=UpgradePhase.ACTIVATING,
        occurred_at=datetime.now(UTC),
        outcome="intent",
    )
    with pytest.raises(UpgradeConflict):
        validate_progress(operation, intent, value, ())
    validate_progress(operation, intent, value, (), activation_authorized=True)


def test_activated_failure_cannot_claim_unchanged_source() -> None:
    value = plan()
    operation = value.operation.model_copy(update={"phase": UpgradePhase.VERIFYING})
    activated = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="activation",
        phase=UpgradePhase.ACTIVATING,
        occurred_at=datetime.now(UTC),
        outcome="intent",
    )
    terminal = activated.model_copy(
        update={
            "phase": UpgradePhase.FAILED_UNCHANGED,
            "outcome": "complete",
            "sequence": 2,
            "observed_sha256": operation.source_sha256,
            "evidence": UpgradeEvidence(
                cleanup_verified=True, source_verified=True
            ).model_dump_json(),
        }
    )
    with pytest.raises(UpgradeConflict):
        validate_progress(operation, terminal, value, (activated,))
    recovery = terminal.model_copy(update={"phase": UpgradePhase.RESTORING, "outcome": "intent"})
    validate_progress(operation, recovery, value, (activated,))


def test_success_requires_exact_runtime_fresh_generation_and_cleanup() -> None:
    value = plan()
    operation = value.operation.model_copy(update={"phase": UpgradePhase.COMMITTING})
    evidence = UpgradeEvidence(
        cleanup_verified=True,
        runtime_sha256=value.target.manifest.environment_sha256,
        generated_tokens=10,
        output_sha256="e" * 64,
        model_id=value.model_id,
        model_variant_id=value.model_variant_id,
        model_manifest_sha256=value.model_manifest_sha256,
    )
    receipt = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="commit",
        phase=UpgradePhase.SUCCEEDED,
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=operation.target_sha256,
        evidence=evidence.model_dump_json(),
    )
    activation = receipt.model_copy(
        update={
            "phase": UpgradePhase.ACTIVATING,
            "outcome": "intent",
            "observed_sha256": value.source.manifest_sha256,
            "evidence": "",
        }
    )
    verification = receipt.model_copy(update={"phase": UpgradePhase.VERIFYING, "sequence": 2})
    receipt = receipt.model_copy(update={"sequence": 3})
    history = (activation, verification)
    validate_progress(operation, receipt, value, history)
    with pytest.raises(UpgradeConflict):
        validate_progress(operation, receipt, value, ())
    with pytest.raises(UpgradeConflict):
        validate_progress(
            operation.model_copy(update={"cancel_requested": True}), receipt, value, history
        )
    for replacement in (
        evidence.model_copy(update={"cleanup_verified": False}),
        evidence.model_copy(update={"generated_tokens": 9}),
        evidence.model_copy(update={"runtime_sha256": value.source.manifest.environment_sha256}),
        evidence.model_copy(update={"model_variant_id": value.model_id}),
    ):
        with pytest.raises(UpgradeConflict):
            validate_progress(
                operation,
                receipt.model_copy(update={"evidence": replacement.model_dump_json()}),
                value,
                history,
            )


@pytest.mark.parametrize("phase", [UpgradePhase.ACTIVATING, UpgradePhase.MIGRATING])
@pytest.mark.parametrize("outcome", ["complete", "failed", "unknown"])
def test_non_intent_activation_labels_cannot_bypass_live_grant(
    phase: UpgradePhase, outcome: str
) -> None:
    value = plan()
    operation = value.operation.model_copy(update={"phase": UpgradePhase.STAGED})
    receipt = UpgradeReceipt.model_validate(
        {
            "job_id": operation.job_id,
            "generation": operation.generation,
            "sequence": 1,
            "effect_id": "forged-activation-completion",
            "phase": phase,
            "occurred_at": datetime.now(UTC),
            "outcome": outcome,
        }
    )
    with pytest.raises(UpgradeConflict, match="consumed intent"):
        validate_progress(operation, receipt, value, ())


def test_core_success_requires_a_durable_selection_decision() -> None:
    from upgrade_fixtures import core_plan

    value = core_plan()
    operation = value.operation.model_copy(update={"phase": UpgradePhase.COMMITTING})
    evidence = UpgradeEvidence(
        cleanup_verified=True,
        runtime_sha256=value.target.manifest_sha256,
        release_sha256=value.target.manifest_sha256,
        schema_head=value.schema_head_after,
        service_image_ids={role.service: role.image_id for role in value.target.manifest.services},
        generated_tokens=10,
        output_sha256="e" * 64,
        model_id=value.model_id,
        model_variant_id=value.model_variant_id,
        model_manifest_sha256=value.model_manifest_sha256,
        check="gateway",
    )
    verified = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=2,
        effect_id="core-verified",
        phase=UpgradePhase.VERIFYING,
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=operation.target_sha256,
        evidence=evidence.model_dump_json(),
    )
    activated = verified.model_copy(
        update={
            "phase": UpgradePhase.ACTIVATING,
            "outcome": "intent",
            "sequence": 1,
            "observed_sha256": operation.source_sha256,
            "evidence": "",
        }
    )
    decision = verified.model_copy(update={"phase": UpgradePhase.COMMITTING, "sequence": 3})
    validate_progress(operation, decision, value, (activated, verified))
    terminal = verified.model_copy(update={"phase": UpgradePhase.SUCCEEDED, "sequence": 4})
    with pytest.raises(UpgradeConflict, match="selection decision"):
        validate_progress(operation, terminal, value, (activated, verified))
    validate_progress(operation, terminal, value, (activated, verified, decision))
    with pytest.raises(UpgradeConflict, match="Cancelled"):
        validate_progress(
            operation.model_copy(update={"cancel_requested": True}),
            decision,
            value,
            (activated, verified),
        )


def test_failed_migration_requires_unchanged_structural_identity() -> None:
    from upgrade_fixtures import core_plan

    value = core_plan(schema_changed=True)
    operation = value.operation.model_copy(update={"phase": UpgradePhase.MIGRATING})
    intent = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="migration",
        phase=UpgradePhase.MIGRATING,
        occurred_at=datetime.now(UTC),
        outcome="intent",
    )
    evidence = UpgradeEvidence(
        source_verified=True,
        cleanup_verified=True,
        migration_exit_code=1,
        schema_head=value.initial_schema_head,
        schema_before_sha256=value.initial_schema_sha256,
        schema_after_sha256=value.initial_schema_sha256,
        service_image_ids={item.service: item.image_id for item in value.source.manifest.services},
    )
    terminal = intent.model_copy(
        update={
            "sequence": 2,
            "phase": UpgradePhase.FAILED_UNCHANGED,
            "outcome": "complete",
            "observed_sha256": operation.source_sha256,
            "evidence": evidence.model_dump_json(),
        }
    )
    validate_progress(operation, terminal, value, (intent,))
    for digest in (None, "f" * 64):
        changed = evidence.model_copy(update={"schema_after_sha256": digest})
        with pytest.raises(UpgradeConflict):
            validate_progress(
                operation,
                terminal.model_copy(update={"evidence": changed.model_dump_json()}),
                value,
                (intent,),
            )
