"""Internally bound Studio child runs; separate from public harness admission."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    AgentRunRow,
    AgentRunTransitionRow,
    EvaluationAttemptRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAdapterRow,
    TrainingJobRow,
)
from coire_api.evaluation.authorization import authorize_live_evaluation_action, reject_self_judge
from coire_api.evaluations import validate_target
from coire_api.gateway.targets import ModelNotFoundError, RegistryTarget, resolve_target
from coire_core.errors import EvaluationConflict, EvaluationForbidden
from coire_core.models.acquisition import VariantState
from coire_core.models.evaluation import (
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkload,
    SuiteKind,
    canonical_digest,
)
from coire_core.models.evaluation_inputs import training_scan_memory_bytes
from coire_core.models.harness import ProfileName, TaskClass
from coire_core.models.registry import ModelState
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRunState,
    RunLimits,
    RunResourceUsage,
    RunTokenScope,
)
from coire_core.settings import Settings


async def resolve_checkpoint_instance_target(
    session: AsyncSession, instance: ModelInstanceRow
) -> RegistryTarget:
    """Resolve only the persisted, owned candidate placement before its child exists."""
    from coire_api.evaluation.checkpoints import require_checkpoint_run_owner
    from coire_api.evaluation.training import require_checkpoint_pause
    from coire_core.errors import CoireError

    try:
        attempt = (
            await session.scalars(
                select(EvaluationAttemptRow).where(
                    EvaluationAttemptRow.instance_id == instance.id,
                    EvaluationAttemptRow.owns_instance.is_(True),
                    EvaluationAttemptRow.state != "released",
                )
            )
        ).one_or_none()
        parent = await session.get(EvaluationRunRow, attempt.run_id) if attempt else None
        adapter = await session.get(TrainingAdapterRow, instance.adapter_id)
        if (
            attempt is None
            or parent is None
            or parent.state not in {"preparing", "reserving", "running", "collecting"}
            or adapter is None
            or adapter.purpose != "evaluation"
            or adapter.state != "ready"
            or adapter.visibility != "admin_only"
            or adapter.verified
            or adapter.evaluation_trigger_id is None
            or parent.data_snapshot is None
            or parent.data_snapshot["job_id"] != adapter.source_job_id
            or parent.data_snapshot["checkpoint_id"] != str(adapter.source_checkpoint_id)
        ):
            raise ModelNotFoundError
        authority = Principal.model_validate(parent.authorization_snapshot)
        if await authorize_live_evaluation_action(session, authority) != parent.owner_user_id:
            raise ModelNotFoundError
        workload = EvaluationWorkload.model_validate(attempt.workload)
        validate_phase_binding(parent, attempt, workload)
        target = workload.target.target
        node = await session.get(NodeRow, attempt.node_id)
        if (
            workload.phase != "candidate"
            or target.adapter_id != adapter.id
            or target.model_id != instance.model_id
            or target.variant_id != instance.variant_id
            or node is None
            or instance.policy != f"single:{node.name}"
            or instance.fallback_instance_id is not None
            or workload.deadline <= datetime.now(UTC)
        ):
            raise ModelNotFoundError
        await require_checkpoint_run_owner(session, parent)
        job = await session.get(TrainingJobRow, adapter.source_job_id, populate_existing=True)
        if job is None:
            raise ModelNotFoundError
        trigger = await require_checkpoint_pause(session, job, adapter.evaluation_trigger_id)
        if trigger.group_id != parent.group_id or trigger.phase != "evaluating":
            raise ModelNotFoundError
        await validate_target(session, target)
        base = await resolve_target(session, target.model_id, authority, target.variant_id)
        if (
            base.identity is None
            or base.identity.base_manifest_sha256 != target.base_manifest_sha256
            or base.model.backend != workload.target.engine_backend
            or adapter.manifest_sha256 != target.adapter_manifest_sha256
        ):
            raise ModelNotFoundError
        return RegistryTarget(base.model, base.variant, adapter, target)
    except (CoireError, ValueError, LookupError):
        raise ModelNotFoundError from None


async def stop_children(session: AsyncSession, run_id: str) -> None:
    from coire_api.run_tokens import revoke_run_token
    from coire_api.runs import transition

    children = (
        await session.scalars(
            select(AgentRunRow)
            .join(EvaluationAttemptRow, EvaluationAttemptRow.agent_run_id == AgentRunRow.id)
            .where(EvaluationAttemptRow.run_id == run_id)
        )
    ).all()
    for child in children:
        await revoke_run_token(session, child.id)
        if (
            child.state not in TERMINAL_RUN_STATES
            and child.state is not AgentRunState.KILL_REQUESTED
        ):
            await transition(session, child, AgentRunState.KILL_REQUESTED, "evaluation stopped")


def validate_phase_binding(
    parent: EvaluationRunRow, attempt: EvaluationAttemptRow, workload: EvaluationWorkload
) -> None:
    if workload.training is not None:
        from coire_core.models.training import parse_resolved_training_spec

        snapshot = parent.data_snapshot
        binding = workload.training
        if snapshot is None or (
            binding.job_id != snapshot["job_id"]
            or str(binding.checkpoint_id) != snapshot["checkpoint_id"]
            or binding.completed_update != snapshot["completed_update"]
            or binding.resolved_spec_sha256 != snapshot["resolved_sha256"]
        ):
            raise EvaluationForbidden("Training input lineage differs from its frozen parent")
        resolved = parse_resolved_training_spec(snapshot["resolved_spec"])
        frozen = {source.dataset_id: source for source in resolved.datasets}
        if (
            binding.mixture != resolved.spec.data.train
            or binding.seed != resolved.spec.data.train.seed
            or binding.batch_size != resolved.spec.optim.batch_size
            or binding.accumulation_steps != resolved.spec.optim.accumulation_steps
            or any(
                source.dataset_id not in frozen
                or source.source_sha256 != frozen[source.dataset_id].source_sha256
                or source.split_sha256 != frozen[source.dataset_id].split_sha256
                for source in binding.sources
            )
        ):
            raise EvaluationForbidden("Training input identities differ from the resolved recipe")
    suite = EvaluationSuite.model_validate(parent.suite_snapshot)
    subjects = [EvaluationTarget.model_validate(item) for item in parent.subjects]
    expected: EvaluationTarget | None = None
    if (
        workload.phase == "harness"
        and suite.template.kind is SuiteKind.HARNESS
        and workload.subject_index == 0
        and len(subjects) == 1
    ):
        expected = subjects[0]
    elif suite.template.kind is not SuiteKind.HARNESS:
        if workload.phase == "base" and workload.subject_index == 0 and subjects:
            expected = subjects[0]
        elif workload.phase == "candidate" and workload.subject_index == 1 and len(subjects) == 2:
            expected = subjects[1]
        elif (
            workload.phase == "judge"
            and workload.subject_index == 0
            and suite.template.kind is SuiteKind.JUDGE
        ):
            expected = suite.judge
    if (
        expected is None
        or workload.target != expected
        or workload.suite != suite
        or workload.subject_count != len(subjects)
        or workload.evaluation_id != parent.id
        or workload.attempt_id != attempt.id
        or attempt.run_id != parent.id
        or workload.fence != parent.fence
        or attempt.fence != parent.fence
        or attempt.phase != workload.phase
        or attempt.target_sha256 != canonical_digest(expected)
        or parent.started_at is None
        or parent.started_deadline_at is None
        or workload.deadline != parent.started_deadline_at
        or workload.deadline != attempt.deadline_at
        or workload.deadline > parent.execution_deadline_at
        or (workload.pressure is None) != (parent.measurement_id is None)
        or (
            workload.pressure is not None
            and workload.pressure.measurement_id != parent.measurement_id
        )
    ):
        raise EvaluationForbidden("Evaluation workload differs from its frozen parent phase")


async def authorize_evaluation_child(
    session: AsyncSession, child: AgentRunRow
) -> EvaluationWorkload:
    if (
        child.purpose != "evaluation"
        or child.evaluation_attempt_id is None
        or child.task_class is not TaskClass.READ
    ):
        raise EvaluationForbidden()
    attempt = await session.get(
        EvaluationAttemptRow, child.evaluation_attempt_id, populate_existing=True
    )
    if attempt is None or attempt.agent_run_id != child.id:
        raise EvaluationForbidden()
    parent = await session.get(EvaluationRunRow, attempt.run_id, populate_existing=True)
    if (
        parent is None
        or parent.owner_user_id != child.requester_user_id
        or parent.fence != attempt.fence
        or parent.state not in {"preparing", "reserving", "running", "collecting"}
    ):
        raise EvaluationForbidden()
    authority = Principal.model_validate(parent.authorization_snapshot)
    if await authorize_live_evaluation_action(session, authority) != parent.owner_user_id:
        raise EvaluationForbidden()
    workload = EvaluationWorkload.model_validate(attempt.workload)
    validate_phase_binding(parent, attempt, workload)
    if workload.pressure is not None:
        measurement = await session.get(
            EvaluationMeasurementRow, workload.pressure.measurement_id, populate_existing=True
        )
        if (
            measurement is None
            or parent.measurement_id != measurement.id
            or measurement.state != "running"
            or measurement.owner_user_id != parent.owner_user_id
            or measurement.deadline_at <= datetime.now(UTC)
            or measurement.execution.get("run_id") != parent.id
        ):
            raise EvaluationForbidden()
    if (
        workload.run_id != child.id
        or workload.attempt_id != attempt.id
        or workload.evaluation_id != parent.id
        or workload.fence != parent.fence
        or workload.deadline <= datetime.now(UTC)
    ):
        raise EvaluationForbidden()
    if workload.previous_outputs:
        raise EvaluationConflict("Durable evaluation intent must use private evidence references")
    from coire_api.evaluation.checkpoints import require_checkpoint_run_owner
    from coire_core.errors import TrainingConflict

    try:
        await require_checkpoint_run_owner(session, parent)
    except TrainingConflict:
        raise EvaluationForbidden() from None
    scope = RunTokenScope.model_validate(child.token_scope)
    if scope.permitted_targets != (workload.target.target,) or scope.permitted_tools:
        raise EvaluationForbidden()
    subjects = [EvaluationTarget.model_validate(subject).target for subject in parent.subjects]
    reject_self_judge(subjects, workload.suite.judge.target if workload.suite.judge else None)
    model = await session.get(ModelRow, workload.target.target.model_id, populate_existing=True)
    variant = await session.get(
        ModelVariantRow,
        workload.target.target.variant_id,
        populate_existing=True,
    )
    if (
        model is None
        or variant is None
        or model.state is not ModelState.READY
        or model.source != "studio"
        or model.backend != workload.target.engine_backend
        or variant.model_id != model.id
        or variant.state is not VariantState.READY
        or not variant.validated
    ):
        raise EvaluationConflict("Evaluation target is unavailable")
    try:
        await validate_target(session, workload.target.target)
    except (ValueError, LookupError) as error:
        raise EvaluationConflict("Evaluation exact target is unavailable") from error
    if workload.target.target.adapter_id is not None:
        adapter = await session.get(TrainingAdapterRow, workload.target.target.adapter_id)
        if adapter is not None and adapter.purpose == "evaluation":
            from coire_api.evaluation.training import require_checkpoint_pause
            from coire_core.errors import TrainingConflict

            job = await session.get(TrainingJobRow, adapter.source_job_id, populate_existing=True)
            if (
                job is None
                or adapter.evaluation_trigger_id is None
                or parent.data_snapshot is None
                or str(adapter.source_checkpoint_id) != parent.data_snapshot["checkpoint_id"]
            ):
                raise EvaluationForbidden()
            try:
                await require_checkpoint_pause(session, job, adapter.evaluation_trigger_id)
            except TrainingConflict:
                raise EvaluationForbidden("Checkpoint evaluation authority ended") from None
    return workload


async def create_evaluation_child(
    session: AsyncSession,
    parent: EvaluationRunRow,
    attempt: EvaluationAttemptRow,
    settings: Settings,
) -> AgentRunRow:
    workload = EvaluationWorkload.model_validate(attempt.workload)
    if attempt.run_id != parent.id or attempt.fence != parent.fence:
        raise EvaluationConflict("Evaluation attempt is stale")
    if (
        attempt.node_id is None
        or attempt.instance_id is None
        or attempt.sandbox_reservation_id is None
    ):
        raise EvaluationConflict("Evaluation phase has no accounted Studio placement")
    existing = await session.get(AgentRunRow, workload.run_id)
    if existing is not None:
        if existing.evaluation_attempt_id != attempt.id:
            raise EvaluationConflict("Evaluation child identity conflict")
        return existing
    scope = RunTokenScope(
        permitted_model_ids=frozenset({workload.target.target.model_id}),
        permitted_targets=(workload.target.target,),
        spend_limit_tokens=min(
            100_000_000,
            workload.target.context_window
            * workload.suite.template.case_count
            * (12 if workload.phase == "judge" else 1)
            * (workload.pressure.max_iterations if workload.pressure else 1),
        ),
    )
    remaining = int((workload.deadline - datetime.now(UTC)).total_seconds())
    if remaining < 10:
        raise EvaluationConflict("Evaluation deadline leaves no safe launch budget")
    limits = RunLimits(
        timeout_seconds=min(remaining, 1800),
        memory_bytes=max(
            settings.run_default_memory_bytes, training_scan_memory_bytes(workload.training)
        ),
        nano_cpus=settings.run_default_nano_cpus,
        pids_limit=settings.run_default_pids_limit,
        log_bytes=settings.run_max_log_bytes,
        result_bytes=min(8 * 1024**2, settings.run_max_result_bytes),
    )
    child = AgentRunRow(
        id=workload.run_id,
        purpose="evaluation",
        evaluation_attempt_id=attempt.id,
        requester_user_id=parent.owner_user_id,
        node_id=attempt.node_id,
        profile=ProfileName.GENERAL.value,
        primary_model_id=workload.target.target.model_id,
        primary_variant_id=workload.target.target.variant_id,
        primary_adapter_id=workload.target.target.adapter_id,
        workspace_ref=f"eval-{workload.run_id}",
        output_ref=f"eval-output-{workload.run_id}",
        task_class=TaskClass.READ,
        token_scope=scope.model_dump(mode="json"),
        state=AgentRunState.QUEUED,
        limits=limits.model_dump(mode="json"),
        resource_usage=RunResourceUsage().model_dump(mode="json"),
    )
    session.add(child)
    attempt.agent_run_id = child.id
    await session.flush()
    await authorize_evaluation_child(session, child)
    session.add(
        AgentRunTransitionRow(
            run_id=child.id,
            from_state=None,
            to_state=AgentRunState.QUEUED,
            reason="evaluation phase requested",
        )
    )
    return child
