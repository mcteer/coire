"""Durable content-free evaluation orchestration over accounted Studio child runs."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    AgentRunRow,
    EngineProcessRow,
    EvaluationAttemptRow,
    EvaluationEvidenceRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    session_scope,
)
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_api.evaluation.events import append, current_run
from coire_api.evaluation.evidence import EvidenceStore
from coire_api.evaluation.execution import create_evaluation_child, stop_children
from coire_api.evaluation.finalizer import finalize
from coire_api.evaluation.telemetry import observed
from coire_api.instance.service import transition as instance_transition
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import acquire_lease, lock_nodes_for_admission, release_lease
from coire_core.errors import CoireError, EvaluationConflict, EvaluationForbidden
from coire_core.models.engine import EngineState
from coire_core.models.evaluation import (
    TERMINAL_EVALUATION_STATES,
    EvaluationIdentityRequest,
    EvaluationInputFile,
    EvaluationPhase,
    EvaluationPressureBinding,
    EvaluationReason,
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkerResult,
    EvaluationWorkload,
    EvaluationWorkspaceCleanup,
    SuiteKind,
    canonical_digest,
)
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState
from coire_core.models.runs import TERMINAL_RUN_STATES
from coire_core.settings import Settings, get_settings
from coire_scheduler.evaluation_admission import reserve_phase
from coire_scheduler.evaluation_guard import guard_reason

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.evaluations")
ticks = metrics.get_meter("coire.scheduler.evaluations").create_counter(
    "coire_evaluation_reconciliation_ticks_total"
)


def phase_plan(
    suite: EvaluationSuite, subjects: list[EvaluationTarget]
) -> list[tuple[EvaluationPhase, int, EvaluationTarget]]:
    if suite.template.kind is SuiteKind.HARNESS:
        return [("harness", 0, subjects[0])]
    phases: list[tuple[EvaluationPhase, int, EvaluationTarget]] = [("base", 0, subjects[0])]
    if len(subjects) == 2:
        phases.append(("candidate", 1, subjects[1]))
    if suite.template.kind is SuiteKind.JUDGE:
        if suite.judge is None:
            raise EvaluationConflict("Declared judge is unavailable")
        phases.append(("judge", 0, suite.judge))
    return phases


async def request_stop(
    session: AsyncSession, run: EvaluationRunRow, reason: EvaluationReason
) -> None:
    run.safe_failure_code = reason
    if run.state != "cancelling":
        run.state = "cancelling"
        run.version += 1
        await append(session, run)
    await stop_children(session, run.id)


@observed("coire.scheduler.evaluation.cleanup")
async def cleanup_phase(
    session: AsyncSession, attempt: EvaluationAttemptRow, settings: Settings
) -> bool:
    """Never release a sandbox or engine lease before authenticated stop proof."""
    if attempt.state == "released":
        return await cleanup_uncommitted_evidence(session, attempt, settings)
    workload = EvaluationWorkload.model_validate(attempt.workload)
    node = await session.get(NodeRow, attempt.node_id) if attempt.node_id else None
    if node is not None:
        try:
            async with NodeClient(settings) as client:
                # Removal is idempotent and node-owned; it waits for a proven stopped container.
                await client.remove_run(node.name, workload.run_id, kill=True)
                await client.cleanup_evaluation_workspace(
                    node.name,
                    EvaluationWorkspaceCleanup(
                        run_id=workload.run_id, request_sha256=canonical_digest(workload)
                    ),
                )
        except NodeError:
            return False
    elif attempt.sandbox_reservation_id is not None:
        return False
    if attempt.node_id is not None:
        await lock_nodes_for_admission(session, [attempt.node_id])
    if attempt.lease_id is not None:
        await release_lease(session, attempt.lease_id)
    if attempt.owns_instance and attempt.instance_id is not None:
        instance = await session.get(
            ModelInstanceRow, attempt.instance_id, with_for_update=True, populate_existing=True
        )
        if instance is not None:
            if instance.state is InstanceState.READY:
                await instance_transition(
                    session, instance.id, InstanceState.DRAINING, reason="evaluation phase cleanup"
                )
                return False
            if instance.state is InstanceState.REQUESTED:
                await instance_transition(
                    session,
                    instance.id,
                    InstanceState.FAILED,
                    reason="evaluation cancelled before launch",
                )
            elif instance.state not in {InstanceState.STOPPED, InstanceState.FAILED}:
                return False
            engines = (
                await session.scalars(
                    select(EngineProcessRow)
                    .join(InstanceMemberRow, InstanceMemberRow.engine_id == EngineProcessRow.id)
                    .where(InstanceMemberRow.instance_id == instance.id)
                )
            ).all()
            for engine in engines:
                if engine.state is EngineState.STOPPED:
                    continue
                if (
                    engine.state is not EngineState.FAILED
                    or node is None
                    or engine.node_id != node.id
                ):
                    return False
                # FAILED is not stop proof. An interrupted unload can leave a dead
                # process recorded as failed; ask its owner for fresh exact-ID proof.
                try:
                    async with NodeClient(settings) as client:
                        proof = await client.stop_engine(node.name, engine.id)
                except NodeError:
                    return False
                if proof is not None and (
                    proof.engine_id != engine.id or proof.state is not EngineState.STOPPED
                ):
                    return False
                engine.state = EngineState.STOPPED
                engine.stopped_at = datetime.now(UTC)
                engine.state_reason = "evaluation owned engine stop confirmed"
    if not await cleanup_uncommitted_evidence(session, attempt, settings):
        return False
    if attempt.sandbox_reservation_id is not None:
        hold = await session.get(
            MemoryReservationRow, attempt.sandbox_reservation_id, with_for_update=True
        )
        if hold is not None:
            hold.state = MemoryReservationState.RELEASED
    for identity in attempt.resident_lease_ids:
        await release_lease(session, uuid.UUID(identity))
    attempt.state = "released"
    return True


async def cleanup_uncommitted_evidence(
    session: AsyncSession, attempt: EvaluationAttemptRow, settings: Settings
) -> bool:
    """After stop proof, remove only this attempt's unpublished evidence receipt.

    The coordinator holds the parent lock, also required by collection. Retained
    metadata owns its bytes until expiry, including expired/history receipts.
    """
    identity = uuid.uuid5(attempt.id, "evaluation-evidence-v1")
    if await session.get(EvaluationEvidenceRow, identity) is not None:
        return True
    root = Path(settings.training_dataset_dir) / "evaluation-evidence"
    if not root.exists() and not root.is_symlink():
        return True
    try:
        await EvidenceStore(settings).remove(identity)
    except (OSError, CoireError):
        return False
    return True


async def load_evidence(
    session: AsyncSession, run: EvaluationRunRow, settings: Settings
) -> list[tuple[EvaluationWorkload, EvaluationWorkerResult]]:
    """Raw evidence stays inside this step, never in DBOS arguments or returns."""
    rows = (
        await session.scalars(
            select(EvaluationAttemptRow)
            .where(
                EvaluationAttemptRow.run_id == run.id,
                EvaluationAttemptRow.collected_sha256.is_not(None),
            )
            .order_by(EvaluationAttemptRow.ordinal)
        )
    ).all()
    store = EvidenceStore(settings)
    values: list[tuple[EvaluationWorkload, EvaluationWorkerResult]] = []
    for attempt in rows:
        identity = uuid.uuid5(attempt.id, "evaluation-evidence-v1")
        evidence = await session.get(EvaluationEvidenceRow, identity)
        if (
            evidence is None
            or evidence.availability != "present"
            or evidence.expires_at <= datetime.now(UTC)
        ):
            raise EvaluationConflict("Required evaluation evidence is unavailable")
        raw = await store.read(identity, evidence.sha256)
        try:
            workload = EvaluationWorkload.model_validate(attempt.workload)
            result = EvaluationWorkerResult.model_validate_json(raw)
        except ValueError:
            raise EvaluationConflict("Private evaluation evidence has unsupported shape") from None
        values.append((workload, result))
    return values


@observed("coire.scheduler.evaluation.phase")
async def advance(session: AsyncSession, run_id: str, settings: Settings) -> bool:
    snapshot = await current_run(session, run_id, lock=False)
    authority_revoked = False
    try:
        await authorize_live_evaluation_action(
            session, Principal.model_validate(snapshot.authorization_snapshot)
        )
    except EvaluationForbidden:
        authority_revoked = True
    run = await current_run(session, run_id)
    attempts = list(
        (
            await session.scalars(
                select(EvaluationAttemptRow)
                .where(EvaluationAttemptRow.run_id == run.id)
                .order_by(EvaluationAttemptRow.ordinal)
            )
        ).all()
    )
    terminal = run.state in TERMINAL_EVALUATION_STATES
    now = datetime.now(UTC)
    if not terminal and run.state != "cancelling":
        from coire_api.evaluation.checkpoints import require_checkpoint_run_owner
        from coire_core.errors import TrainingConflict

        try:
            await require_checkpoint_run_owner(session, run)
        except TrainingConflict:
            await request_stop(session, run, "cancelled")
        if authority_revoked:
            await request_stop(session, run, "authorization_revoked")
        if run.state != "cancelling":
            if run.started_deadline_at is not None and now >= run.started_deadline_at:
                await request_stop(session, run, "execution_timeout")
            elif run.started_at is None and now >= run.queue_deadline_at:
                await request_stop(session, run, "capacity_timeout")
    if terminal or run.state == "cancelling":
        cleaned = True
        for attempt in attempts:
            if not await cleanup_phase(session, attempt, settings):
                cleaned = False
        run.cleanup_state = "complete" if cleaned else "pending"
        if not terminal:
            terminal_reason = cast(EvaluationReason, run.safe_failure_code or "internal")
            await finalize(
                session,
                run.id,
                fence=run.fence,
                outcome="cancelled"
                if terminal_reason == "cancelled"
                else "timed_out"
                if terminal_reason in {"execution_timeout", "capacity_timeout"}
                else "failed",
                reason=terminal_reason,
            )
        if cleaned and not any(attempt.collected_sha256 for attempt in attempts):
            await session.execute(text("SELECT pg_advisory_xact_lock(170027)"))
            run.evidence_reserved_bytes = 0
        return cleaned
    suite = EvaluationSuite.model_validate(run.suite_snapshot)
    if run.measurement_id is not None:
        measurement = await session.get(
            EvaluationMeasurementRow, run.measurement_id, populate_existing=True
        )
        if measurement is None or measurement.state in {"failed", "cancelled", "succeeded"}:
            await request_stop(session, run, "cancelled")
            return False
        if measurement.execution.get("phase") != "mixed":
            return False
    subjects = [EvaluationTarget.model_validate(item) for item in run.subjects]
    plan = phase_plan(suite, subjects)
    active = next((attempt for attempt in attempts if attempt.state != "released"), None)
    if active is not None and active.collected_sha256 is not None:
        if not await cleanup_phase(session, active, settings):
            return False
        active = None
    if active is None:
        ordinal = len(attempts) + 1
        if ordinal > len(plan):
            try:
                receipts = await load_evidence(session, run, settings)
                failed = next(
                    (worker for _, worker in receipts if worker.outcome != "succeeded"), None
                )
                run.cleanup_state = "complete"
                await finalize(
                    session,
                    run.id,
                    fence=run.fence,
                    outcome=failed.outcome if failed else "succeeded",
                    reason=failed.reason if failed else None,
                    evidence=receipts,
                )
            except CoireError:
                await request_stop(session, run, "invalid_evidence")
                return False
            return True
        phase, index, target = plan[ordinal - 1]
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL, f"coire:evaluation:{run.id}:{run.fence}:{ordinal}"
        )
        from coire_api.evaluation.inputs import freeze_training_inputs

        training_inputs, inputs = await freeze_training_inputs(session, run)
        if phase == "judge":
            for previous in attempts:
                row = await session.get(
                    EvaluationEvidenceRow, uuid.uuid5(previous.id, "evaluation-evidence-v1")
                )
                if row is None:
                    await request_stop(session, run, "evidence_unavailable")
                    return False
                inputs.append(
                    EvaluationInputFile(
                        name=f"evidence-{row.id}.json",
                        sha256=row.sha256,
                        bytes=row.bytes,
                        purpose="previous_outputs",
                    )
                )
        deadline = run.started_deadline_at or min(
            run.execution_deadline_at, now + timedelta(seconds=suite.timeout_seconds)
        )
        workload = EvaluationWorkload(
            evaluation_id=run.id,
            attempt_id=identity,
            run_id=uuid.uuid5(identity, "child"),
            fence=run.fence,
            phase=phase,
            suite=suite,
            target=target,
            subject_index=index,
            subject_count=len(subjects),
            deadline=deadline,
            input_files=inputs,
            training=training_inputs,
            pressure=EvaluationPressureBinding(measurement_id=run.measurement_id)
            if run.measurement_id
            else None,
        )
        active = EvaluationAttemptRow(
            id=identity,
            run_id=run.id,
            phase=phase,
            ordinal=ordinal,
            fence=run.fence,
            target_sha256=canonical_digest(target),
            workload=workload.model_dump(mode="json"),
            deadline_at=deadline,
            state="pending",
        )
        session.add(active)
        await session.flush()
    workload = EvaluationWorkload.model_validate(active.workload)
    if active.sandbox_reservation_id is None:
        if run.started_at is None:
            deadline = min(
                run.execution_deadline_at, now + timedelta(seconds=suite.timeout_seconds)
            )
            workload = workload.model_copy(update={"deadline": deadline})
            active.workload = workload.model_dump(mode="json")
            active.deadline_at = deadline
        # Exactly one active group by default, including gaps between sequential phases.
        await session.execute(text("SELECT pg_advisory_xact_lock(170100)"))
        other = await session.scalar(
            select(EvaluationRunRow.id)
            .where(
                EvaluationRunRow.group_id != run.group_id,
                EvaluationRunRow.started_at.is_not(None),
                EvaluationRunRow.state.notin_(
                    [state.value for state in TERMINAL_EVALUATION_STATES]
                ),
            )
            .limit(1)
        )
        if other is not None or not await reserve_phase(
            session, run, active, workload.target, settings
        ):
            return False
        if run.started_at is None:
            run.started_at = now
            run.started_deadline_at = workload.deadline
        run.state, run.phase = "reserving", active.phase
        run.version += 1
        await append(session, run)
        return False
    reason = await guard_reason(session, active)
    if reason is not None:
        await request_stop(session, run, reason)
        return False
    instance = await session.get(ModelInstanceRow, active.instance_id, populate_existing=True)
    if instance is None or instance.state in {
        InstanceState.FAILED,
        InstanceState.STOPPED,
        InstanceState.DRAINING,
    }:
        await request_stop(session, run, "model_unavailable")
        return False
    if instance.state is not InstanceState.READY:
        return False
    if active.agent_run_id is None:
        members = (
            await session.scalars(
                select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance.id)
            )
        ).all()
        if (
            len(members) != 1
            or members[0].node_id != active.node_id
            or members[0].reservation_id is None
            or members[0].engine_id is None
        ):
            await request_stop(session, run, "invalid_evidence")
            return False
        node = await session.get(NodeRow, active.node_id)
        if node is None or workload.target.variant_slug is None:
            await request_stop(session, run, "runtime_mismatch")
            return False
        try:
            async with NodeClient(settings) as client:
                actual_runtime = await client.evaluation_identity(
                    node.name,
                    EvaluationIdentityRequest(
                        engine_backend=workload.target.engine_backend,
                        engine_id=members[0].engine_id,
                        target=workload.target.target,
                        variant_slug=workload.target.variant_slug,
                        template_override=workload.target.template_override,
                        capability_profile=workload.target.capability_profile,
                    ),
                )
        except NodeError:
            await request_stop(session, run, "runtime_mismatch")
            return False
        if actual_runtime != workload.target.runtime:
            await request_stop(session, run, "runtime_mismatch")
            return False
        lease = await acquire_lease(
            session,
            members[0].reservation_id,
            f"evaluation:{active.id}",
            ttl_seconds=max(1, (workload.deadline - now).total_seconds()),
        )
        active.lease_id = lease.id
        await create_evaluation_child(session, run, active, settings)
        run.state = "running"
        run.version += 1
        await append(session, run)
        return False
    child = await session.get(AgentRunRow, active.agent_run_id, populate_existing=True)
    if child is None or (child.state in TERMINAL_RUN_STATES and active.collected_sha256 is None):
        await request_stop(session, run, "invalid_evidence")
    return False


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1)
async def evaluation_tick(run_id: str) -> bool:
    with tracer.start_as_current_span(
        "coire.scheduler.evaluation.advance",
        attributes={"run_id": run_id},
        record_exception=False,
        set_status_on_exception=False,
    ):
        async with session_scope() as session:
            complete = await advance(session, run_id, get_settings())
        ticks.add(1, {"outcome": "complete" if complete else "pending"})
        return complete


@DBOS.workflow(name="coire.evaluation.workflow", max_recovery_attempts=100)
async def evaluation_workflow(run_id: str) -> None:
    while not await evaluation_tick(run_id):
        await DBOS.sleep_async(1)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1)
async def final_evaluation_trigger_tick(identity: str) -> bool:
    from coire_api.evaluation.training import reconcile_final_trigger

    async with session_scope() as session:
        return await reconcile_final_trigger(session, uuid.UUID(identity), settings=get_settings())


@DBOS.workflow(name="coire.evaluation.training.final", max_recovery_attempts=100)
async def final_evaluation_trigger_workflow(identity: str) -> None:
    while not await final_evaluation_trigger_tick(identity):
        await DBOS.sleep_async(1)
