"""Bounded recovery of completed checkpoint pins, independent of admission switches."""

import uuid
from datetime import datetime

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    EvaluationCheckpointPinRow,
    EvaluationRunRow,
    ModelInstanceRow,
    TrainingAdapterRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
)
from coire_core.models.evaluation import TERMINAL_EVALUATION_STATES
from coire_core.models.instance import InstanceState

tracer = trace.get_tracer("coire.api.evaluation")
released = metrics.get_meter("coire.api.evaluation").create_counter(
    "coire_evaluation_checkpoint_pins_released_total"
)


async def release_trigger_pins(session: AsyncSession, identity: uuid.UUID, *, now: datetime) -> int:
    with tracer.start_as_current_span(
        "coire.api.evaluation.pins.release", record_exception=False, set_status_on_exception=False
    ):
        snapshot = await session.get(TrainingEvaluationTriggerRow, identity)
        if snapshot is None:
            return 0
        # Retention and checkpoint control use the same job-first lock ordering.
        job = await session.get(
            TrainingJobRow, snapshot.job_id, with_for_update=True, populate_existing=True
        )
        trigger = await session.get(
            TrainingEvaluationTriggerRow, identity, with_for_update=True, populate_existing=True
        )
        if (
            job is None
            or trigger is None
            or trigger.phase != "complete"
            or trigger.completed_at is None
            or job.evaluation_pause_trigger_id == identity
        ):
            return 0
        if trigger.group_id is not None:
            unsafe = await session.scalar(
                select(EvaluationRunRow.id)
                .where(
                    EvaluationRunRow.group_id == trigger.group_id,
                    EvaluationRunRow.state.notin_(
                        [state.value for state in TERMINAL_EVALUATION_STATES]
                    )
                    | (EvaluationRunRow.cleanup_state != "complete"),
                )
                .limit(1)
            )
            if unsafe is not None:
                return 0
        active_adapters = select(ModelInstanceRow.adapter_id).where(
            ModelInstanceRow.state.notin_([InstanceState.STOPPED, InstanceState.FAILED])
        )
        unsafe_adapter = await session.scalar(
            select(TrainingAdapterRow.id)
            .where(
                TrainingAdapterRow.purpose == "evaluation",
                TrainingAdapterRow.evaluation_trigger_id == identity,
                (TrainingAdapterRow.state != "retired")
                | TrainingAdapterRow.id.in_(active_adapters),
            )
            .limit(1)
        )
        if unsafe_adapter is not None:
            return 0
        pins = (
            await session.scalars(
                select(EvaluationCheckpointPinRow)
                .where(
                    EvaluationCheckpointPinRow.trigger_id == identity,
                    EvaluationCheckpointPinRow.released_at.is_(None),
                )
                .with_for_update()
            )
        ).all()
        for pin in pins:
            pin.released_at = now
        if pins:
            await write_principal_audit(
                session,
                principal=Principal.model_validate(job.authorization_snapshot),
                action="evaluation.pin.release",
                target_type="training_evaluation_trigger",
                target_id=str(identity),
                context={"job_id": job.id, "released_pins": len(pins)},
            )
            released.add(len(pins))
        return len(pins)


async def sweep_completed_pins(session: AsyncSession, *, now: datetime) -> int:
    identities = (
        await session.scalars(
            select(TrainingEvaluationTriggerRow.id)
            .join(
                EvaluationCheckpointPinRow,
                EvaluationCheckpointPinRow.trigger_id == TrainingEvaluationTriggerRow.id,
            )
            .where(
                TrainingEvaluationTriggerRow.phase == "complete",
                TrainingEvaluationTriggerRow.completed_at.is_not(None),
                EvaluationCheckpointPinRow.released_at.is_(None),
            )
            .distinct()
            .order_by(TrainingEvaluationTriggerRow.id)
            .limit(100)
        )
    ).all()
    count = 0
    for identity in identities:
        count += await release_trigger_pins(session, identity, now=now)
    return count
