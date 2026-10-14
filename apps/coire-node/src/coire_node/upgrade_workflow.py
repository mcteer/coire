"""Durable native preparation stages executed by the independent recovery owner."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from opentelemetry import trace

from coire_core.errors import UpgradeCollectivePending, UpgradeConflict, UpgradeUnavailable
from coire_core.models import ProbeOutcome
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrades import (
    NativeAbortIntent,
    NativeRecoveryIntent,
    NativeSmokeAttempt,
    NativeSmokeCommand,
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
from coire_node.upgrade_child import run_guard_smoke
from coire_node.upgrade_cleanup import SMOKE_STAGES, reconcile_smoke_cleanup
from coire_node.upgrade_executor import (
    activate_native_candidate,
    prepare_native_candidate,
    restore_native_source,
)
from coire_node.upgrade_finalize import (
    finalize_candidate,
    finalize_restored_source,
    finalize_unchanged_source,
)
from coire_node.upgrade_guard import NativeGuardJournal, NativeGuardTransport

tracer = trace.get_tracer("coire.node.upgrade_workflow")


def qualify_native_pair(
    journal: NativeGuardJournal,
    transport: NativeGuardTransport,
    proof: UpgradeEvidence,
    *,
    stage: Literal["candidate", "source"],
    deadline_at: datetime,
) -> UpgradeEvidence:
    plan = journal.plan
    if not (plan.source.manifest.paired_required or plan.target.manifest.paired_required):
        return proof
    name = stage + "-collective.json"
    result = journal.store.read(name, NodeUpgradeCollectiveResult)
    if result is None:
        if proof.runtime_sha256 is None:
            raise UpgradeUnavailable("Native paired serving has no measured runtime")
        try:
            result = transport.collective(
                plan,
                proof.runtime_sha256,
                deadline_at=deadline_at,
                stage=stage,
            )
        except UpgradeUnavailable:
            # An unavailable live-owner check is not a failed measurement.
            # Fixed scope/command identities replay already retained worker
            # results; an unresolved dispatch never permits another launch.
            raise UpgradeCollectivePending(
                "Native collective qualification is unavailable"
            ) from None
        journal.store.write(name, result, replace=False)
    if (
        result.job_id != plan.operation.job_id
        or result.generation != plan.operation.generation
        or result.plan_sha256 != plan.authority_sha256()
        or result.stage != stage
        or result.deadline_at != deadline_at
        or len(result.observations) != 3
        or any(item.outcome is not ProbeOutcome.SUCCEEDED for item in result.observations)
        or datetime.now(UTC) >= deadline_at
    ):
        raise UpgradeUnavailable("Native paired serving collective qualification failed")
    pair = (
        (proof.runtime_sha256, plan.peer_runtime_sha256)
        if plan.operation.node == "coire-edge-a"
        else (plan.peer_runtime_sha256, proof.runtime_sha256)
    )
    if any(
        (item.node_a, item.node_b) != ("coire-edge-a", "coire-edge-b")
        or (item.runtime_sha256_a, item.runtime_sha256_b) != pair
        or item.transport.value != "jaccl"
        or item.observed_at >= deadline_at
        for item in result.observations
    ):
        raise UpgradeConflict("Native retained collective proof differs from the exact pair")
    return proof.model_copy(
        update={
            "collective_verified": True,
            "peer_runtime_sha256": plan.peer_runtime_sha256,
        }
    )


def advance_guard_step(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Dispatch durable progress; a cancelled or expired activation restores its source."""
    current = journal.current()
    latest = journal.receipts.latest()
    if latest is not None and latest.phase is UpgradePhase.SUCCEEDED:
        return finalize_candidate(settings, journal, transport)
    if current.phase == "recovery_required" and current.activation_started_at is None:
        # An unclosed pre-activation abort never becomes permission to select
        # either runtime. Keep its original budget and all uncertain holds.
        aborted = journal.store.read("abort.json", NativeAbortIntent)
        if aborted is None or (
            aborted.captured_at,
            aborted.deadline_at,
        ) != (current.recovery_started_at, current.recovery_deadline_at):
            raise UpgradeUnavailable("Native unchanged recovery authority differs")
        return None
    if current.phase == "source_verified":
        latest = journal.receipts.latest()
        if (
            latest is not None
            and latest.phase is not UpgradePhase.ROLLED_BACK
            and current.recovery_deadline_at is not None
            and datetime.now(UTC) >= current.recovery_deadline_at
        ):
            return record_recovery_required(journal)
        return finalize_restored_source(settings, journal, transport)
    cancelled = journal.root.read("cancel.json", NodeUpgradeCancel)
    if cancelled is not None and cancelled.generation != journal.plan.operation.generation:
        raise UpgradeConflict("Native cancellation generation differs")
    if current.phase == "prepared":
        command = journal.root.read("command.json", NodeUpgradeCommand)
        if command is not None and command.activation_grant is not None:
            # Consumed Core intent is logical activation even if delivery was
            # delayed. An expired grant permits recorded-source recovery only.
            recovery_only = (
                cancelled is not None
                or journal.store.read("abort.json", NativeAbortIntent) is not None
                or datetime.now(UTC) >= command.activation_grant.expires_at
            )
            current = journal.record_activation(command, recovery_only=recovery_only)
    if current.phase in {"activation_intent", "candidate_selected", "candidate_verified"} and (
        cancelled is not None
        or (
            current.readiness_deadline_at is not None
            and datetime.now(UTC) >= current.readiness_deadline_at
        )
    ):
        observed_failure = datetime.now(UTC)
        if current.readiness_deadline_at is not None:
            observed_failure = min(observed_failure, current.readiness_deadline_at)
        current = journal.begin_restoration(now=observed_failure)
    if current.phase in {"restoring", "recovery_required", "source_selected"}:
        latest = journal.receipts.latest()
        if (
            current.phase == "source_selected"
            and latest is not None
            and latest.phase is UpgradePhase.VERIFYING_SOURCE
            and latest.outcome == "complete"
        ):
            return verify_source_step(settings, journal, transport)
        if (
            current.recovery_deadline_at is not None
            and datetime.now(UTC) >= current.recovery_deadline_at
        ):
            return record_recovery_required(journal)
        restored = restore_guard_step(settings, journal, transport)
        return restored or verify_source_step(settings, journal, transport)
    if current.phase == "prepared":
        aborted = journal.store.read("abort.json", NativeAbortIntent)
        if cancelled is not None or aborted is not None:
            reason = (
                aborted.reason
                if aborted is not None
                else (
                    "authorization_revoked"
                    if cancelled is not None and cancelled.reason == "authorization_revoked"
                    else "cancelled"
                )
            )
            return finalize_unchanged_source(settings, journal, transport, reason)
        if datetime.now(UTC) >= journal.plan.maintenance.drain_deadline_at:
            return finalize_unchanged_source(settings, journal, transport, "drain_deadline")
        try:
            prepared = prepare_guard_step(settings, journal, transport)
            return prepared or activate_guard_step(settings, journal, transport)
        except UpgradeUnavailable:
            latest = journal.receipts.latest()
            if latest is not None and latest.phase is UpgradePhase.STAGED:
                return finalize_unchanged_source(settings, journal, transport, "candidate_failed")
            raise
    if current.phase in {"activation_intent", "candidate_selected"}:
        try:
            if latest is not None and (
                (latest.phase is UpgradePhase.VERIFYING and latest.outcome == "complete")
                or (latest.phase is UpgradePhase.COMMITTING and latest.outcome == "intent")
            ):
                return finalize_candidate(settings, journal, transport)
            activated = activate_guard_step(settings, journal, transport)
            return activated or verify_candidate_step(settings, journal, transport)
        except UpgradeUnavailable:
            # A failed candidate cannot renew readiness on retry. Capture recovery
            # once before the error leaves the worker; uncertain cleanup stays fenced.
            journal.begin_restoration()
            raise
    return None


def record_recovery_required(journal: NativeGuardJournal) -> UpgradeReceipt | None:
    """Retain uncertain restoration after its deadline; never open a maintenance fence."""
    with journal.store.ownership("intent.lock"):
        current = journal.current()
        if current.phase not in {
            "restoring",
            "source_selected",
            "source_verified",
            "recovery_required",
        }:
            raise UpgradeConflict("Native recovery-required ownership differs")
        if current.recovery_deadline_at is None or datetime.now(UTC) < current.recovery_deadline_at:
            raise UpgradeConflict("Native recovery-required deadline has not elapsed")
        latest = journal.receipts.latest()
        if latest is None:
            raise UpgradeUnavailable("Native recovery-required progress is absent")
        receipt: UpgradeReceipt | None = None
        if latest.phase is not UpgradePhase.RECOVERY_REQUIRED:
            receipt = UpgradeReceipt(
                job_id=journal.plan.operation.job_id,
                generation=journal.plan.operation.generation,
                sequence=latest.sequence + 1,
                phase=UpgradePhase.RECOVERY_REQUIRED,
                effect_id="native-recovery-required",
                occurred_at=datetime.now(UTC),
                outcome="unknown",
                reason="native_recovery_deadline",
                restoration="pending",
            )
            journal.receipts.append(receipt)
        if current.phase != "recovery_required":
            journal.store.write(
                "intent.json",
                NativeRecoveryIntent.model_validate(
                    current.model_dump(mode="json")
                    | {"phase": "recovery_required", "updated_at": datetime.now(UTC)}
                ),
            )
            journal._record("recovery_required")
        return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_source_serving_step", record_exception=False, set_status_on_exception=False
)
def verify_source_step(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Record fresh restored-source serving without opening maintenance or releasing holds."""
    current = journal.current()
    latest = journal.receipts.latest()
    if (
        current.phase != "source_selected"
        or latest is None
        or latest.phase is not UpgradePhase.VERIFYING_SOURCE
    ):
        return None
    plan = journal.plan
    claim = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root)).read(
        "claim.json", NativeUpgradeClaim
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
    ):
        raise UpgradeConflict("Native source serving operation is not the published owner")
    deadline = current.recovery_deadline_at
    if deadline is None:
        raise UpgradeUnavailable("Native source serving has no captured recovery deadline")
    if latest.outcome == "complete":
        proof = UpgradeEvidence.model_validate_json(latest.evidence)
        if (
            latest.occurred_at >= deadline
            or latest.observed_sha256 != plan.source.manifest_sha256
            or proof.runtime_sha256 != current.source.runtime.environment_sha256
            or proof.check != "node"
            or proof.source_verified is not True
            or proof.cleanup_verified is not True
            or proof.generated_tokens is None
            or proof.generated_tokens < 10
            or proof.output_sha256 is None
            or (proof.model_id, proof.model_variant_id, proof.model_manifest_sha256)
            != (plan.model_id, plan.model_variant_id, plan.model_manifest_sha256)
        ):
            raise UpgradeUnavailable("Native restored source retained serving proof differs")
        journal.store.write(
            "intent.json",
            NativeRecoveryIntent.model_validate(
                current.model_dump(mode="json")
                | {"phase": "source_verified", "updated_at": datetime.now(UTC)}
            ),
        )
        journal._record("source_verified")
        return None
    if latest.outcome != "intent" or datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable("Native source serving captured recovery deadline elapsed")
    status = transport.status(plan, deadline_at=deadline)
    if not status.bootstrap_ready or (
        status.job_id,
        status.generation,
        status.release_sha256,
        status.runtime_sha256,
        status.environment_id,
    ) != (
        plan.operation.job_id,
        plan.operation.generation,
        plan.source.manifest_sha256,
        current.source.runtime.environment_sha256,
        current.source.runtime.environment_id,
    ):
        return None
    proof = run_guard_smoke(
        settings,
        journal,
        NativeSmokeCommand(
            plan=plan,
            runtime_sha256=current.source.runtime.environment_sha256,
            stage="source-serving",
            deadline_at=deadline,
        ),
    ).model_copy(update={"source_verified": True})
    try:
        proof = qualify_native_pair(journal, transport, proof, stage="source", deadline_at=deadline)
    except UpgradeUnavailable:
        # Serving restoration is distinct from paired eligibility. Core records
        # this failed qualification before it can release accepted maintenance.
        proof = proof.model_copy(
            update={
                "collective_verified": False,
                "peer_runtime_sha256": plan.peer_runtime_sha256,
            }
        )
    if datetime.now(UTC) >= deadline or journal.current() != current:
        raise UpgradeUnavailable("Native source serving ownership or captured deadline changed")
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=latest.sequence + 1,
        phase=UpgradePhase.VERIFYING_SOURCE,
        effect_id="native-source-serving",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=plan.source.manifest_sha256,
        evidence=proof.model_dump_json(),
    )
    journal.receipts.append(receipt)
    # Intent advancement is reconciled from this durable receipt on the next
    # tick, including a restart after the receipt fsync but before advancement.
    return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_restore_step", record_exception=False, set_status_on_exception=False
)
def restore_guard_step(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Reconcile captured restoration without extending its deadline or forcing drain."""
    plan = journal.plan
    claim = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root)).read(
        "claim.json", NativeUpgradeClaim
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
    ):
        raise UpgradeConflict("Native restoration operation is not the published owner")
    current = journal.current()
    if current.phase not in {"restoring", "recovery_required", "source_selected"}:
        return None
    deadline = current.recovery_deadline_at
    if deadline is None or datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable("Native restoration captured recovery deadline elapsed")
    latest = journal.receipts.latest()
    if latest is None:
        raise UpgradeUnavailable("Native restoration progress is absent")
    if latest.phase not in {UpgradePhase.RESTORING, UpgradePhase.VERIFYING_SOURCE}:
        receipt = UpgradeReceipt(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            sequence=latest.sequence + 1,
            phase=UpgradePhase.RESTORING,
            effect_id="native-restoring",
            occurred_at=datetime.now(UTC),
            outcome="intent",
            observed_sha256=plan.target.manifest_sha256,
        )
        journal.receipts.append(receipt)
        return receipt
    if current.phase != "source_selected":
        for stage in SMOKE_STAGES:
            if not reconcile_smoke_cleanup(settings, journal, stage, deadline):
                return None
        # A broken HTTP server does not prove a live candidate is idle. The
        # restorer asks for fresh drain only if its exact process remains alive;
        # a positively absent process can restore without that server responding.
        restore_native_source(
            settings,
            journal,
            refresh_candidate_inventory=lambda: transport.inventory(
                plan, current.candidate.environment_sha256, deadline_at=deadline
            ),
        )
    if latest.phase is UpgradePhase.VERIFYING_SOURCE:
        return None
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=latest.sequence + 1,
        phase=UpgradePhase.VERIFYING_SOURCE,
        effect_id="native-verifying-source",
        occurred_at=datetime.now(UTC),
        outcome="intent",
        observed_sha256=plan.source.manifest_sha256,
    )
    journal.receipts.append(receipt)
    return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_prepare_step", record_exception=False, set_status_on_exception=False
)
def prepare_guard_step(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Advance one preparation boundary; active work and cancellation never force drain."""
    plan = journal.plan
    job = plan.operation
    bootstrap = Path(settings.upgrade_bootstrap_root)
    if not bootstrap.exists():
        raise UpgradeUnavailable("Native preparation has no independent bootstrap")
    claim = PrivateUpgradeStore(bootstrap).read("claim.json", NativeUpgradeClaim)
    if (
        claim is None
        or claim.state != "active"
        or (claim.job_id, claim.generation, claim.node, claim.plan_sha256)
        != (job.job_id, job.generation, job.node, plan.authority_sha256())
    ):
        raise UpgradeConflict("Native preparation operation is not the published owner")
    intent = journal.current()
    if intent.phase != "prepared":
        return None
    cancelled = journal.root.read("cancel.json", NodeUpgradeCancel)
    if cancelled is not None:
        if cancelled.generation != job.generation:
            raise UpgradeConflict("Native cancellation generation differs")
        return None  # The recovery owner reconciles cancellation separately.
    latest = journal.receipts.latest()
    phase = latest.phase if latest is not None else UpgradePhase.ACCEPTED
    if phase not in {
        UpgradePhase.ACCEPTED,
        UpgradePhase.PREFLIGHT,
        UpgradePhase.FENCING,
        UpgradePhase.DRAINING,
        UpgradePhase.STAGED,
    }:
        return None
    if datetime.now(UTC) >= plan.maintenance.drain_deadline_at:
        raise UpgradeUnavailable("Native preparation captured drain deadline elapsed")
    evidence: UpgradeEvidence | None = None
    outcome: Literal["complete", "intent"] = "complete"
    if phase is UpgradePhase.ACCEPTED:
        status = transport.status(plan, deadline_at=plan.maintenance.drain_deadline_at)
        if not status.bootstrap_ready or (
            status.node,
            status.release_sha256,
            status.runtime_sha256,
            status.environment_id,
        ) != (
            job.node,
            plan.source.manifest_sha256,
            intent.source.runtime.environment_sha256,
            intent.source.runtime.environment_id,
        ):
            raise UpgradeUnavailable("Native preparation source is not physically verified")
        destination = UpgradePhase.PREFLIGHT
        evidence = UpgradeEvidence(
            check="source",
            runtime_sha256=intent.source.runtime.environment_sha256,
            source_verified=True,
        )
    elif phase in {UpgradePhase.PREFLIGHT, UpgradePhase.FENCING}:
        fence = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades").read(
            "maintenance.json", UpgradeMaintenance
        )
        if fence is None or not fence.matches_plan(plan):
            return None
        destination = (
            UpgradePhase.FENCING if phase is UpgradePhase.PREFLIGHT else UpgradePhase.DRAINING
        )
        outcome = "intent" if destination is UpgradePhase.DRAINING else "complete"
    elif phase is UpgradePhase.DRAINING:
        snapshot = transport.inventory(plan, intent.source.runtime.environment_sha256)
        if not snapshot.idle(REQUIRED_OWNERS):
            return None
        status = transport.status(plan, deadline_at=plan.maintenance.drain_deadline_at)
        candidate = prepare_native_candidate(settings, plan, status, snapshot)
        if candidate != intent.candidate:
            raise UpgradeUnavailable("Native preparation staged candidate differs")
        destination = UpgradePhase.STAGED
        evidence = UpgradeEvidence(
            check="node", runtime_sha256=candidate.environment_sha256, artifacts_verified=True
        )
    else:
        attempt = journal.store.read("candidate-smoke.json", NativeSmokeAttempt)
        deadline = (
            attempt.deadline_at
            if attempt is not None
            else min(
                plan.maintenance.drain_deadline_at,
                datetime.now(UTC) + timedelta(seconds=job.timing.readiness_s),
            )
        )
        evidence = run_guard_smoke(
            settings,
            journal,
            NativeSmokeCommand(
                plan=plan,
                runtime_sha256=intent.candidate.environment_sha256,
                stage="candidate-smoke",
                deadline_at=deadline,
            ),
        )
        destination = UpgradePhase.SMOKE_BEFORE
    # Every retry starts from the last fsynced receipt. No response or heartbeat
    # constitutes progress, and an activation intent can come only from Core's grant.
    receipt = UpgradeReceipt(
        job_id=job.job_id,
        generation=job.generation,
        sequence=latest.sequence + 1 if latest is not None else 1,
        phase=destination,
        effect_id="native-" + destination.value,
        occurred_at=datetime.now(UTC),
        outcome=outcome,
        observed_sha256=plan.source.manifest_sha256,
        evidence=evidence.model_dump_json() if evidence is not None else "",
    )
    journal.receipts.append(receipt)
    return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_serving_step", record_exception=False, set_status_on_exception=False
)
def verify_candidate_step(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Require the selected node and a fresh serving child before qualification."""
    current = journal.current()
    latest = journal.receipts.latest()
    if (
        current.phase != "candidate_selected"
        or latest is None
        or latest.phase is not UpgradePhase.VERIFYING
        or latest.outcome != "intent"
    ):
        return None
    if journal.root.read("cancel.json", NodeUpgradeCancel) is not None:
        return None
    plan = journal.plan
    claim = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root)).read(
        "claim.json", NativeUpgradeClaim
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
    ):
        raise UpgradeConflict("Native serving operation is not the published owner")
    deadline = current.readiness_deadline_at
    if deadline is None or datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable("Native serving captured readiness deadline elapsed")
    try:
        status = transport.status(plan, deadline_at=deadline)
    except UpgradeUnavailable:
        # The former node was deliberately stopped. Connection refusal while
        # launchd starts its replacement is absence of readiness, rather than a
        # failed serving smoke. Keep the original window and fence; the outer
        # step captures restoration at its original expiry.
        return None
    if not status.bootstrap_ready or (
        status.job_id,
        status.generation,
        status.release_sha256,
        status.runtime_sha256,
        status.environment_id,
    ) != (
        plan.operation.job_id,
        plan.operation.generation,
        plan.target.manifest_sha256,
        current.candidate.environment_sha256,
        current.candidate.environment_id,
    ):
        return None  # launchd may still be starting the newly selected node.
    proof = run_guard_smoke(
        settings,
        journal,
        NativeSmokeCommand(
            plan=plan,
            runtime_sha256=current.candidate.environment_sha256,
            stage="candidate-serving",
            deadline_at=deadline,
        ),
    )
    try:
        proof = qualify_native_pair(
            journal, transport, proof, stage="candidate", deadline_at=deadline
        )
    except UpgradeCollectivePending:
        # Keep the original readiness epoch and fence. Expiry/cancellation is
        # handled by advance_guard_step; a definite failed result still restores.
        return None
    if datetime.now(UTC) >= deadline or journal.current() != current:
        raise UpgradeUnavailable("Native serving ownership or captured deadline changed")
    receipt = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        sequence=latest.sequence + 1,
        phase=UpgradePhase.VERIFYING,
        effect_id="native-serving",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=plan.target.manifest_sha256,
        evidence=proof.model_dump_json(),
    )
    journal.receipts.append(receipt)
    return receipt


@tracer.start_as_current_span(
    "coire.node.upgrade_activation_step", record_exception=False, set_status_on_exception=False
)
def activate_guard_step(
    settings: Settings, journal: NativeGuardJournal, transport: NativeGuardTransport
) -> UpgradeReceipt | None:
    """Consume already granted Core authority and reconcile one native selection."""
    plan = journal.plan
    job = plan.operation
    claim = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root)).read(
        "claim.json", NativeUpgradeClaim
    )
    if (
        claim is None
        or claim.state != "active"
        or (claim.job_id, claim.generation, claim.node, claim.plan_sha256)
        != (job.job_id, job.generation, job.node, plan.authority_sha256())
    ):
        raise UpgradeConflict("Native activation operation is not the published owner")
    if journal.root.read("cancel.json", NodeUpgradeCancel) is not None:
        return None
    current = journal.current()
    if current.phase == "prepared":
        command = journal.root.read("command.json", NodeUpgradeCommand)
        if command is None or command.activation_grant is None:
            return None
        current = journal.record_activation(command)
    if current.phase not in {"activation_intent", "candidate_selected"}:
        return None
    latest = journal.receipts.latest()
    if latest is None or latest.phase not in {UpgradePhase.ACTIVATING, UpgradePhase.VERIFYING}:
        raise UpgradeUnavailable("Native activation receipt is unreconciled")
    if latest.phase is UpgradePhase.VERIFYING:
        return None
    deadline = current.readiness_deadline_at
    if deadline is None or datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable("Native activation captured readiness deadline elapsed")
    if current.phase == "activation_intent":
        inventory = transport.inventory(
            plan, current.source.runtime.environment_sha256, deadline_at=deadline
        )
        # The consumed command retains its original peer observation. New peer
        # reads are scoped observations, never replacement activation authority.
        activate_native_candidate(
            settings,
            journal,
            inventory,
            refresh_inventory=lambda: transport.inventory(
                plan, current.source.runtime.environment_sha256, deadline_at=deadline
            ),
            refresh_peer_inventory=(
                (lambda: transport.peer_inventory(plan, deadline_at=deadline))
                if plan.peer_runtime_sha256 is not None
                else None
            ),
        )
    receipt = UpgradeReceipt(
        job_id=job.job_id,
        generation=job.generation,
        sequence=latest.sequence + 1,
        phase=UpgradePhase.VERIFYING,
        effect_id="native-verifying",
        occurred_at=datetime.now(UTC),
        outcome="intent",
        observed_sha256=plan.target.manifest_sha256,
    )
    journal.receipts.append(receipt)
    return receipt
