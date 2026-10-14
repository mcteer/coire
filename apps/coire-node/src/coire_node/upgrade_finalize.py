"""Native operation closure requires physical staging and serving-owner cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeAbortIntent,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeUpgradeCancel,
    NodeUpgradeCommand,
    UpgradeEvidence,
    UpgradeMaintenance,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.maintenance import REQUIRED_OWNERS
from coire_node.upgrade_cleanup import SMOKE_STAGES, reconcile_smoke_cleanup, verify_local_cleanup
from coire_node.upgrade_executor import NativeDiskReservation

if TYPE_CHECKING:
    from coire_node.upgrade_guard import NativeGuardJournal, NativeGuardTransport

tracer = trace.get_tracer("coire.node.upgrade_finalize")


def finalize_unchanged_source(
    settings: Settings,
    journal: NativeGuardJournal,
    transport: NativeGuardTransport,
    reason: Literal["cancelled", "authorization_revoked", "drain_deadline", "candidate_failed"],
) -> UpgradeReceipt | None:
    """Capture one abort budget and preserve known existing work on an untouched source."""
    plan = journal.plan
    with journal.root.ownership("command.lock"):
        current = journal.current()
        if (
            current.phase not in {"prepared", "unchanged_verified"}
            or current.activation_started_at is not None
        ):
            raise UpgradeConflict("Native unchanged closure has activation authority")
        latest = journal.receipts.latest()
        if latest is not None and latest.phase in {
            UpgradePhase.FAILED_UNCHANGED,
            UpgradePhase.CANCELLED_UNCHANGED,
        }:
            proof = UpgradeEvidence.model_validate_json(latest.evidence)
            if (
                latest.outcome != "complete"
                or latest.observed_sha256 != plan.source.manifest_sha256
                or proof.runtime_sha256 != current.source.runtime.environment_sha256
                or proof.source_verified is not True
                or proof.cleanup_verified is not True
            ):
                raise UpgradeUnavailable("Native unchanged terminal proof differs")
            verify_local_cleanup(settings, journal, None, proof)
            if current.phase == "prepared":
                journal.store.write(
                    "intent.json",
                    NativeRecoveryIntent.model_validate(
                        current.model_dump(mode="json")
                        | {"phase": "unchanged_verified", "updated_at": datetime.now(UTC)}
                    ),
                )
                journal._record("unchanged_verified")
            return None
        command = journal.root.read("command.json", NodeUpgradeCommand)
        if command is not None and command.activation_grant is not None:
            journal.record_activation(command, recovery_only=True)
            return None
        aborted = journal.store.read("abort.json", NativeAbortIntent)
        if aborted is None:
            now = datetime.now(UTC)
            capture = now
            if reason == "drain_deadline":
                capture = min(capture, plan.maintenance.drain_deadline_at)
            elif reason == "candidate_failed" and latest is not None and latest.outcome == "failed":
                capture = min(capture, latest.occurred_at)
            elif reason in {"cancelled", "authorization_revoked"}:
                marker = journal.root.read("cancel.json", NodeUpgradeCancel)
                if marker is not None:
                    if marker.generation != plan.operation.generation:
                        raise UpgradeConflict("Native abort cancellation epoch differs")
                    capture = min(
                        capture,
                        datetime.fromtimestamp(
                            journal.root.path("cancel.json").stat().st_mtime, UTC
                        ),
                    )
            aborted = NativeAbortIntent(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                plan_sha256=plan.authority_sha256(),
                reason=reason,
                captured_at=capture,
                deadline_at=capture + timedelta(seconds=plan.operation.timing.recovery_s),
            )
            journal.store.write("abort.json", aborted, replace=False)
        if (aborted.job_id, aborted.generation, aborted.plan_sha256) != (
            plan.operation.job_id,
            plan.operation.generation,
            plan.authority_sha256(),
        ):
            raise UpgradeConflict("Native unchanged cleanup authority differs")
    deadline = aborted.deadline_at
    if datetime.now(UTC) >= deadline:
        with journal.store.ownership("intent.lock"):
            current = journal.current()
            if current.activation_started_at is not None:
                raise UpgradeConflict("Native unchanged cleanup acquired activation authority")
            latest = journal.receipts.latest()
            receipt = None
            if latest is None or latest.phase is not UpgradePhase.RECOVERY_REQUIRED:
                receipt = UpgradeReceipt(
                    job_id=plan.operation.job_id,
                    generation=plan.operation.generation,
                    sequence=latest.sequence + 1 if latest is not None else 1,
                    phase=UpgradePhase.RECOVERY_REQUIRED,
                    effect_id="native-unchanged-recovery-required",
                    occurred_at=datetime.now(UTC),
                    outcome="unknown",
                    reason="native_unchanged_cleanup_deadline",
                    restoration="pending",
                )
                journal.receipts.append(receipt)
            if current.phase != "recovery_required":
                journal.store.write(
                    "intent.json",
                    NativeRecoveryIntent.model_validate(
                        current.model_dump(mode="json")
                        | {
                            "phase": "recovery_required",
                            "recovery_started_at": aborted.captured_at,
                            "recovery_deadline_at": deadline,
                            "updated_at": datetime.now(UTC),
                        }
                    ),
                )
                journal._record("recovery_required")
            return receipt
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))

    def authority() -> None:
        claim = root.read("claim.json", NativeUpgradeClaim)
        fence = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
            "maintenance.json", UpgradeMaintenance
        )
        if (
            claim is None
            or claim.state != "active"
            or (claim.job_id, claim.generation, claim.node, claim.plan_sha256)
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.operation.node,
                plan.authority_sha256(),
            )
            or fence is None
            or not fence.matches_plan(plan)
        ):
            raise UpgradeConflict("Native unchanged cleanup owner or fence differs")
        if journal.current() != current or datetime.now(UTC) >= deadline:
            raise UpgradeConflict("Native unchanged closure ownership or deadline changed")

    authority()
    for stage in SMOKE_STAGES:
        if not reconcile_smoke_cleanup(settings, journal, stage, deadline):
            return None
    status = transport.status(plan, deadline_at=deadline)
    if not status.bootstrap_ready or (status.release_sha256, status.runtime_sha256) != (
        plan.source.manifest_sha256,
        current.source.runtime.environment_sha256,
    ):
        return None
    snapshot = transport.inventory(
        plan, current.source.runtime.environment_sha256, deadline_at=deadline
    )
    if not snapshot.reconciled(REQUIRED_OWNERS):
        return None
    reservation = (
        Path(settings.upgrade_environment_root) / ".upgrade-reservations" / plan.operation.job_id
    )
    authority()
    if reservation.exists() or reservation.is_symlink():
        ledger = NativeDiskReservation(reservation, plan)
        released = ledger.release()
        if released is None or released.state != "released":
            raise UpgradeUnavailable("Native unchanged staging cleanup remains unknown")
    proof = UpgradeEvidence(
        check="source",
        runtime_sha256=current.source.runtime.environment_sha256,
        source_verified=True,
        cleanup_verified=True,
    )
    verify_local_cleanup(settings, journal, None, proof)
    with journal.root.ownership("command.lock"):
        command = journal.root.read("command.json", NodeUpgradeCommand)
        if command is not None and command.activation_grant is not None:
            journal.record_activation(command, recovery_only=True)
            return None
        authority()
        latest = journal.receipts.latest()
        receipt = UpgradeReceipt(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            sequence=latest.sequence + 1 if latest is not None else 1,
            phase=UpgradePhase.CANCELLED_UNCHANGED
            if aborted.reason == "cancelled"
            else UpgradePhase.FAILED_UNCHANGED,
            effect_id="native-unchanged-closed",
            occurred_at=datetime.now(UTC),
            outcome="complete",
            observed_sha256=plan.source.manifest_sha256,
            reason="native_" + aborted.reason,
            evidence=proof.model_dump_json(),
        )
        journal.receipts.append(receipt)
        journal.store.write(
            "intent.json",
            NativeRecoveryIntent.model_validate(
                current.model_dump(mode="json")
                | {"phase": "unchanged_verified", "updated_at": datetime.now(UTC)}
            ),
        )
        journal._record("unchanged_verified")
        return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_source_finalize", record_exception=False, set_status_on_exception=False
)
def finalize_restored_source(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Close restored-source progress only after all operation-owned resources are clear."""
    current = journal.current()
    latest = journal.receipts.latest()
    if current.phase != "source_verified" or latest is None:
        return None
    if latest.phase is UpgradePhase.ROLLED_BACK:
        return None
    if latest.phase is not UpgradePhase.VERIFYING_SOURCE or latest.outcome != "complete":
        raise UpgradeUnavailable("Native restored source has no durable serving verification")
    plan = journal.plan
    deadline = current.recovery_deadline_at
    if deadline is None or datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable("Native source closure captured recovery deadline elapsed")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    fence_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")

    def authority() -> None:
        claim = root.read("claim.json", NativeUpgradeClaim)
        fence = fence_store.read("maintenance.json", UpgradeMaintenance)
        if (
            claim is None
            or claim.state != "active"
            or (claim.job_id, claim.generation, claim.node, claim.plan_sha256)
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.operation.node,
                plan.authority_sha256(),
            )
            or fence is None
            or not fence.matches_plan(plan)
            or journal.current() != current
            or datetime.now(UTC) >= deadline
        ):
            raise UpgradeConflict("Native restored source closure ownership changed")

    authority()
    proof = UpgradeEvidence.model_validate_json(latest.evidence)
    if (
        latest.observed_sha256 != plan.source.manifest_sha256
        or proof.check != "node"
        or proof.source_verified is not True
        or proof.runtime_sha256 != current.source.runtime.environment_sha256
        or proof.generated_tokens is None
        or proof.generated_tokens < 10
        or proof.output_sha256 is None
        or (proof.model_id, proof.model_variant_id, proof.model_manifest_sha256)
        != (plan.model_id, plan.model_variant_id, plan.model_manifest_sha256)
    ):
        raise UpgradeUnavailable("Native restored source serving proof differs")
    for stage in SMOKE_STAGES:
        if not reconcile_smoke_cleanup(settings, journal, stage, deadline):
            return None
    status = transport.status(plan, deadline_at=deadline)
    if not status.bootstrap_ready or (status.release_sha256, status.runtime_sha256) != (
        plan.source.manifest_sha256,
        current.source.runtime.environment_sha256,
    ):
        return None
    snapshot = transport.inventory(
        plan, current.source.runtime.environment_sha256, deadline_at=deadline
    )
    if not snapshot.idle(REQUIRED_OWNERS):
        return None
    authority()
    ledger = NativeDiskReservation(
        Path(settings.upgrade_environment_root) / ".upgrade-reservations" / plan.operation.job_id,
        plan,
    )
    released = ledger.release()
    if released is None or released.state != "released":
        raise UpgradeUnavailable("Native restored source staging ownership is unknown")
    verify_local_cleanup(settings, journal, "source-serving", proof)
    snapshot = transport.inventory(
        plan, current.source.runtime.environment_sha256, deadline_at=deadline
    )
    if not snapshot.idle(REQUIRED_OWNERS):
        return None
    authority()
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=latest.sequence + 1,
        phase=UpgradePhase.ROLLED_BACK,
        effect_id="native-source-closed",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=plan.source.manifest_sha256,
        evidence=proof.model_copy(update={"cleanup_verified": True}).model_dump_json(),
        restoration="verified",
    )
    journal.receipts.append(receipt)
    return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_candidate_finalize", record_exception=False, set_status_on_exception=False
)
def finalize_candidate(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Close candidate progress only after all operation-owned resources are clear."""
    current = journal.current()
    latest = journal.receipts.latest()
    if (
        current.phase not in {"candidate_selected", "candidate_verified", "committed"}
        or latest is None
    ):
        return None
    if latest.phase is UpgradePhase.SUCCEEDED:
        proof = UpgradeEvidence.model_validate_json(latest.evidence)
        if (
            latest.outcome != "complete"
            or latest.observed_sha256 != journal.plan.target.manifest_sha256
            or proof.cleanup_verified is not True
            or proof.runtime_sha256 != current.candidate.environment_sha256
        ):
            raise UpgradeUnavailable("Native candidate terminal proof differs")
        verify_local_cleanup(settings, journal, "candidate-serving", proof)
        if current.phase != "committed":
            with journal.root.ownership("command.lock"):
                if journal.current() != current:
                    raise UpgradeConflict("Native candidate terminal ownership changed")
                journal.store.write(
                    "intent.json",
                    NativeRecoveryIntent.model_validate(
                        current.model_dump(mode="json")
                        | {"phase": "committed", "updated_at": datetime.now(UTC)}
                    ),
                )
                journal._record("committed")
        return None
    verification: UpgradeReceipt | None = latest
    if latest.phase is UpgradePhase.COMMITTING and latest.outcome == "intent":
        verification = next(
            (
                receipt
                for receipt in reversed(journal.receipts.page(max(0, latest.sequence - 32)))
                if receipt.phase is UpgradePhase.VERIFYING and receipt.outcome == "complete"
            ),
            None,
        )
    elif latest.phase is not UpgradePhase.VERIFYING or latest.outcome != "complete":
        raise UpgradeUnavailable("Native candidate has no durable serving verification")
    if verification is None:
        raise UpgradeUnavailable("Native candidate retained serving verification is absent")
    plan = journal.plan
    deadline = current.readiness_deadline_at
    if deadline is None or datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable("Native source closure captured readiness deadline elapsed")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root))
    fence_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")

    def authority() -> None:
        claim = root.read("claim.json", NativeUpgradeClaim)
        fence = fence_store.read("maintenance.json", UpgradeMaintenance)
        if (
            claim is None
            or claim.state != "active"
            or (claim.job_id, claim.generation, claim.node, claim.plan_sha256)
            != (
                plan.operation.job_id,
                plan.operation.generation,
                plan.operation.node,
                plan.authority_sha256(),
            )
            or fence is None
            or not fence.matches_plan(plan)
            or journal.current() != current
            or datetime.now(UTC) >= deadline
        ):
            raise UpgradeConflict("Native candidate closure ownership changed")

    authority()
    proof = UpgradeEvidence.model_validate_json(verification.evidence)
    if (
        verification.observed_sha256 != plan.target.manifest_sha256
        or proof.check != "node"
        or proof.runtime_sha256 != current.candidate.environment_sha256
        or proof.generated_tokens is None
        or proof.generated_tokens < 10
        or proof.output_sha256 is None
        or (proof.model_id, proof.model_variant_id, proof.model_manifest_sha256)
        != (plan.model_id, plan.model_variant_id, plan.model_manifest_sha256)
    ):
        raise UpgradeUnavailable("Native candidate serving proof differs")
    if (plan.source.manifest.paired_required or plan.target.manifest.paired_required) and (
        proof.collective_verified is not True
        or proof.peer_runtime_sha256 != plan.peer_runtime_sha256
    ):
        return None
    if journal.root.read("cancel.json", NodeUpgradeCancel) is not None:
        return None
    if latest.phase is UpgradePhase.VERIFYING:
        with journal.root.ownership("command.lock"):
            authority()
            if journal.root.read("cancel.json", NodeUpgradeCancel) is not None:
                return None
            receipt = UpgradeReceipt(
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                sequence=latest.sequence + 1,
                phase=UpgradePhase.COMMITTING,
                effect_id="native-committing",
                occurred_at=datetime.now(UTC),
                outcome="intent",
                observed_sha256=plan.target.manifest_sha256,
            )
            journal.receipts.append(receipt)
            return receipt
    for stage in SMOKE_STAGES:
        if not reconcile_smoke_cleanup(settings, journal, stage, deadline):
            return None
    status = transport.status(plan, deadline_at=deadline)
    if not status.bootstrap_ready or (status.release_sha256, status.runtime_sha256) != (
        plan.target.manifest_sha256,
        current.candidate.environment_sha256,
    ):
        return None
    snapshot = transport.inventory(plan, current.candidate.environment_sha256, deadline_at=deadline)
    if not snapshot.idle(REQUIRED_OWNERS):
        return None
    authority()
    ledger = NativeDiskReservation(
        Path(settings.upgrade_environment_root) / ".upgrade-reservations" / plan.operation.job_id,
        plan,
    )
    released = ledger.release()
    if released is None or released.state != "released":
        raise UpgradeUnavailable("Native candidate staging ownership is unknown")
    verify_local_cleanup(settings, journal, "candidate-serving", proof)
    snapshot = transport.inventory(plan, current.candidate.environment_sha256, deadline_at=deadline)
    if not snapshot.idle(REQUIRED_OWNERS):
        return None
    authority()
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=latest.sequence + 1,
        phase=UpgradePhase.SUCCEEDED,
        effect_id="native-candidate-closed",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=plan.target.manifest_sha256,
        evidence=proof.model_copy(update={"cleanup_verified": True}).model_dump_json(),
    )
    with journal.root.ownership("command.lock"):
        if journal.root.read("cancel.json", NodeUpgradeCancel) is not None:
            return None
        authority()
        journal.receipts.append(receipt)
        journal.store.write(
            "intent.json",
            NativeRecoveryIntent.model_validate(
                current.model_dump(mode="json")
                | {"phase": "committed", "updated_at": datetime.now(UTC)}
            ),
        )
        journal._record("committed")
    return receipt
