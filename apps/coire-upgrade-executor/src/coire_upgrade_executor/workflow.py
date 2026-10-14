"""Finite durable Core steps; physical Source recovery precedes API reconnection."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from opentelemetry import trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    CoreRuntimeSelection,
    UpgradeEvidence,
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeSmokeResult,
    core_upgrade_required_owners,
)

from .executor import CoreExecutor
from .replay import replay_receipts

tracer = trace.get_tracer("coire.upgrade_executor.workflow")


def emit(
    executor: CoreExecutor,
    phase: UpgradePhase,
    *,
    outcome: Literal["intent", "complete", "failed", "unknown"] = "complete",
    evidence: UpgradeEvidence | None = None,
    source: bool = True,
    restoration: Literal["not_required", "pending", "verified", "failed"] = "not_required",
    occurred_at: datetime | None = None,
    reason: str | None = None,
) -> UpgradeReceipt:
    """Fsync progress before transport; never discard a lost acknowledgment."""
    journal = executor.journal
    latest = journal.receipts.latest()
    receipt = UpgradeReceipt(
        job_id=journal.plan.operation.job_id,
        generation=journal.plan.operation.generation,
        sequence=latest.sequence + 1 if latest is not None else 1,
        effect_id="core-" + phase.value.replace("_", "-") + "-" + outcome,
        phase=phase,
        occurred_at=occurred_at or datetime.now(UTC),
        outcome=outcome,
        observed_sha256=(journal.plan.source if source else journal.plan.target).manifest_sha256,
        evidence=evidence.model_dump_json() if evidence is not None else "",
        restoration=restoration,
        reason=reason,
    )
    executor.authority.verify()
    journal.receipts.append(receipt)
    return receipt


async def freeze_source(executor: CoreExecutor) -> None:
    """Source preservation does not depend on a usable candidate artifact."""
    executor.authority.verify()
    observations = tuple(
        await asyncio.gather(
            *(
                executor.docker.inspect_verified_source(
                    name, executor.prepared_root, dependency_states={}
                )
                for name in executor.docker.source_names
            )
        )
    )
    executor.authority.verify()
    executor.journal.freeze_source(observations)
    await executor.verify_schema(restored=True)


async def serving_evidence(
    executor: CoreExecutor, selection: CoreRuntimeSelection, smoke: UpgradeSmokeResult
) -> UpgradeEvidence:
    source = selection.release_id == executor.journal.plan.source.release_id
    schema = await executor.verify_schema(restored=source)
    return UpgradeEvidence(
        release_sha256=selection.manifest_sha256,
        runtime_sha256=smoke.runtime_sha256,
        schema_head=schema.schema_head,
        service_image_ids={
            item.container.service: item.container.image_id for item in selection.services
        },
        model_id=smoke.model_id,
        model_variant_id=smoke.model_variant_id,
        model_manifest_sha256=smoke.model_manifest_sha256,
        generated_tokens=smoke.generated_tokens,
        output_sha256=smoke.output_sha256,
        check=smoke.check,
        source_verified=True if source else None,
        cleanup_verified=True,
        configuration_verified=True,
    )


async def recover_step(executor: CoreExecutor) -> UpgradeReceipt | None:
    """Restore exact Source containers even when API and scheduler are unavailable."""
    journal = executor.journal
    state = journal.capture_recovery()
    latest = journal.receipts.latest()
    if latest is not None and latest.phase in TERMINAL_UPGRADE_PHASES:
        await replay_receipts(executor.client, journal.receipts)
        await executor.commit_runtime(dependency_states={})
        return None
    if state.recovery_deadline is None:
        raise UpgradeConflict("Core recovery has no retained deadline")
    activated = any(item.grant.boundary == "runtime" for item in state.activations)
    if (
        activated
        and latest is not None
        and latest.phase is UpgradePhase.VERIFYING_SOURCE
        and latest.outcome == "complete"
        and latest.occurred_at < state.recovery_deadline
    ):
        await executor.restored_runtime(dependency_states={}, terminal_replay=True)
        return emit(
            executor,
            UpgradePhase.ROLLED_BACK,
            evidence=UpgradeEvidence.model_validate_json(latest.evidence),
            restoration="verified",
        )
    if datetime.now(UTC) >= state.recovery_deadline:
        if latest is not None and latest.phase is UpgradePhase.RECOVERY_REQUIRED:
            # Retaining recovery ownership must not hide the durable failure
            # receipt from the control plane after its first local emission.
            await replay_receipts(executor.client, journal.receipts)
            return None
        return emit(
            executor,
            UpgradePhase.RECOVERY_REQUIRED,
            outcome="unknown",
            restoration="failed",
            reason="recovery_deadline",
        )
    if activated and (
        latest is None
        or latest.phase
        not in {
            UpgradePhase.RESTORING,
            UpgradePhase.VERIFYING_SOURCE,
            UpgradePhase.RECOVERY_REQUIRED,
        }
    ):
        return emit(
            executor,
            UpgradePhase.RESTORING,
            outcome="intent",
            restoration="pending",
            occurred_at=state.recovery_captured_at,
        )
    if activated and latest is not None and latest.phase is UpgradePhase.VERIFYING_SOURCE:
        # Physical recovery has already brought the gateway back. Ingest its
        # retained restoration progress before asking for the scoped probe.
        await replay_receipts(executor.client, journal.receipts)
        # A failed or lost smoke reply cannot leave subsequent attempts using
        # an expired census. Refresh exact physical Source identities before
        # each scoped request, under the unchanged recovery deadline.
        await executor.restored_runtime(dependency_states={})
        smoke = await executor.client.smoke(restored=True, deadline=state.recovery_deadline)
        selection = await executor.restored_runtime(dependency_states={})
        evidence = await serving_evidence(executor, selection, smoke)
        if datetime.now(UTC) >= state.recovery_deadline:
            raise UpgradeUnavailable("Source serving proof exceeded original recovery deadline")
        return emit(
            executor, UpgradePhase.VERIFYING_SOURCE, evidence=evidence, restoration="verified"
        )
    await executor.restore_cohort(dependency_states={})
    selection = await executor.cleanup_candidates(dependency_states={})
    if activated:
        return emit(
            executor, UpgradePhase.VERIFYING_SOURCE, outcome="intent", restoration="pending"
        )
    # No runtime activation means there is no user-work termination or fresh
    # engine probe during a timed-out drain. Source config/schema and owned
    # cleanup must still be positively verified before declaring unchanged.
    schema = await executor.verify_schema(restored=True)
    operation = await executor.client.status()
    migration = journal.store.read("migration-proof.json", UpgradeMigrationExecution)
    not_started = journal.store.read("migration-not-started.json", UpgradeMigrationNotStarted)
    evidence = UpgradeEvidence(
        release_sha256=selection.manifest_sha256,
        schema_head=schema.schema_head,
        service_image_ids={
            item.container.service: item.container.image_id for item in selection.services
        },
        source_verified=True,
        configuration_verified=True,
        cleanup_verified=True,
        migration_exit_code=migration.exit_code if migration is not None else None,
        schema_before_sha256=migration.schema_before_sha256
        if migration is not None
        else not_started.schema_sha256
        if not_started is not None
        else None,
        schema_after_sha256=migration.schema_after_sha256
        if migration is not None
        else not_started.schema_sha256
        if not_started is not None
        else None,
        migration_not_started_sha256=not_started.sha256() if not_started is not None else None,
    )
    if datetime.now(UTC) >= state.recovery_deadline:
        raise UpgradeUnavailable("Unchanged Source proof exceeded original recovery deadline")
    return emit(
        executor,
        UpgradePhase.CANCELLED_UNCHANGED
        if operation.cancel_requested
        else UpgradePhase.FAILED_UNCHANGED,
        evidence=evidence,
        restoration="verified",
        reason="cancelled" if operation.cancel_requested else "candidate_failed",
    )


async def advance_core_step(executor: CoreExecutor) -> UpgradeReceipt | None:
    """One resumable step under the independent kernel owner; no DBOS in this service."""
    with tracer.start_as_current_span("coire.upgrade_executor.workflow_step") as span:
        journal = executor.journal
        state = journal.state()
        operation = journal.plan.operation
        latest = journal.receipts.latest()
        span.set_attribute("job_id", operation.job_id)
        span.set_attribute("coire.upgrade.generation", operation.generation)
        span.set_attribute("coire.upgrade.kind", operation.kind.value)
        span.set_attribute(
            "coire.upgrade.phase", (latest.phase if latest else operation.phase).value
        )
        span.set_attribute("coire.upgrade.owner_kind", "executor")
        span.set_attribute("coire.upgrade.recovering", state.recovery_captured_at is not None)
        if state.recovery_captured_at is not None:
            return await recover_step(executor)
        try:
            await replay_receipts(executor.client, journal.receipts)
            operation = await executor.client.status()
        except (UpgradeUnavailable, UpgradeForbidden, UpgradeConflict) as error:
            if (
                isinstance(error, (UpgradeForbidden, UpgradeConflict))
                and not (
                    latest is not None
                    and latest.phase is UpgradePhase.COMMITTING
                    and latest.outcome == "complete"
                )
                and executor.authority.claim.state != "committed"
            ):
                # Rejected progress or a revoked transport credential cannot authorize more
                # candidate effects or prevent the retained Source owner acting.
                journal.capture_recovery()
                return await recover_step(executor)
            # API reconnection cannot extend a consumed runtime's original
            # budget. A published selection is a separate reconciliation
            # boundary and must never be blindly undone after a lost reply.
            if (
                executor.authority.claim.state != "committed"
                and latest is not None
                and latest.phase not in TERMINAL_UPGRADE_PHASES
                and latest.phase is not UpgradePhase.COMMITTING
                and not (latest.phase is UpgradePhase.VERIFYING and latest.outcome == "complete")
                and journal.expired_forward_deadlines()
            ):
                journal.capture_recovery()
                return await recover_step(executor)
            # A consumed activation must remain executable while its API is
            # being replaced; new grants and terminal selection still need it.
            if (
                latest is not None
                and latest.phase is UpgradePhase.ACTIVATING
                and latest.outcome == "intent"
                and any(item.grant.boundary == "runtime" for item in state.activations)
            ):
                try:
                    await executor.replace_cohort(dependency_states={})
                    return emit(executor, UpgradePhase.VERIFYING, outcome="intent", source=False)
                except CoireError:
                    journal.capture_recovery()
                    raise
            raise
        if operation.finished_at is not None:
            if (
                latest is None
                or latest.phase is not operation.phase
                or latest.sequence != operation.receipt_sequence
            ):
                raise UpgradeConflict("Terminal API progress differs from retained local receipts")
            await executor.commit_runtime(dependency_states={})
            return None
        if operation.cancel_requested:
            journal.capture_recovery()
            return await recover_step(executor)
        if latest is not None and latest.phase in TERMINAL_UPGRADE_PHASES:
            raise UpgradeConflict("Terminal local progress has no matching ingested acknowledgment")
        try:
            return await forward_step(executor)
        except CoireError as error:
            if (
                latest is not None
                and latest.phase is UpgradePhase.COMMITTING
                and latest.outcome == "complete"
            ):
                # The API has acknowledged the commit decision before this
                # effect. Reconcile publication, retaining the fence; a lost
                # reply or partial publication cannot authorize Source rollback.
                raise
            if (
                isinstance(error, UpgradeUnavailable)
                and latest is not None
                and latest.phase
                in {UpgradePhase.PREFLIGHT, UpgradePhase.FENCING, UpgradePhase.DRAINING}
                and datetime.now(UTC) < journal.plan.maintenance.drain_deadline_at
            ):
                return None
            journal.capture_recovery()
            raise


async def forward_step(executor: CoreExecutor) -> UpgradeReceipt | None:
    journal = executor.journal
    latest = journal.receipts.latest()
    if latest is None:
        await freeze_source(executor)
        await executor.docker.preflight_cohort(
            journal, executor.authority, prepared_root=executor.prepared_root, dependency_states={}
        )
        return emit(executor, UpgradePhase.PREFLIGHT)
    phase = latest.phase
    if phase is UpgradePhase.PREFLIGHT:
        await executor.client.inventory()
        return emit(executor, UpgradePhase.FENCING)
    if phase is UpgradePhase.FENCING:
        return emit(executor, UpgradePhase.DRAINING, outcome="intent")
    if phase is UpgradePhase.DRAINING:
        if datetime.now(UTC) >= journal.plan.maintenance.drain_deadline_at:
            journal.capture_recovery()
            return await recover_step(executor)
        inventory = await executor.client.inventory()
        if not inventory.idle(core_upgrade_required_owners(journal.plan)):
            return None
        await executor.docker.preflight_cohort(
            journal, executor.authority, prepared_root=executor.prepared_root, dependency_states={}
        )
        await executor.verify_schema(restored=True)
        return emit(
            executor,
            UpgradePhase.STAGED,
            evidence=UpgradeEvidence(
                release_sha256=journal.plan.target.manifest_sha256,
                artifacts_verified=True,
                configuration_verified=True,
                service_image_ids={
                    role.service: role.image_id for role in journal.plan.target.manifest.services
                },
                backup_validation_sha256=journal.plan.backup.validation_sha256
                if journal.plan.backup
                else None,
            ),
        )
    if phase is UpgradePhase.STAGED:
        return (await executor.consume_boundary(migration=journal.plan.needs_migration)).intent
    if phase is UpgradePhase.MIGRATING:
        if latest.outcome == "complete":
            return (await executor.consume_boundary()).intent
        proof = await executor.migrate(dependency_states={})
        return emit(
            executor,
            UpgradePhase.MIGRATING,
            evidence=UpgradeEvidence(
                schema_head=proof.schema_after_head,
                schema_before_sha256=proof.schema_before_sha256,
                schema_after_sha256=proof.schema_after_sha256,
                migration_exit_code=proof.exit_code,
            ),
        )
    if phase is UpgradePhase.ACTIVATING:
        await executor.replace_cohort(dependency_states={})
        return emit(executor, UpgradePhase.VERIFYING, outcome="intent", source=False)
    if phase is UpgradePhase.VERIFYING:
        if latest.outcome == "complete":
            await executor.selected_runtime(dependency_states={}, terminal_replay=True)
            return emit(executor, UpgradePhase.COMMITTING, outcome="intent", source=False)
        await executor.selected_runtime(dependency_states={})
        state = journal.state()
        last = next(
            window
            for window in state.service_windows
            if window.service == journal.plan.operation.cohort[-1]
        )
        deadline = (
            min(last.deadline_at, state.outage_deadline)
            if state.outage_deadline
            else last.deadline_at
        )
        smoke = await executor.client.smoke(deadline=deadline)
        selection = await executor.selected_runtime(dependency_states={})
        evidence = await serving_evidence(executor, selection, smoke)
        if datetime.now(UTC) >= deadline:
            raise UpgradeUnavailable("Target serving proof exceeded original readiness deadline")
        return emit(
            executor,
            UpgradePhase.VERIFYING,
            evidence=evidence,
            source=False,
        )
    if phase is UpgradePhase.COMMITTING:
        verification = next(
            receipt
            for receipt in reversed(journal.receipts.page(max(0, latest.sequence - 32)))
            if receipt.phase is UpgradePhase.VERIFYING and receipt.outcome == "complete"
        )
        if latest.outcome != "complete":
            await executor.selected_runtime(dependency_states={}, terminal_replay=True)
            return emit(
                executor,
                UpgradePhase.COMMITTING,
                evidence=UpgradeEvidence.model_validate_json(verification.evidence),
                source=False,
            )
        await executor.commit_runtime(dependency_states={})
        return emit(
            executor,
            UpgradePhase.SUCCEEDED,
            evidence=UpgradeEvidence.model_validate_json(verification.evidence),
            source=False,
        )
    raise UpgradeConflict("Core worker encountered an unsupported retained phase")
