"""Bounded metadata-only projections of pending obligations and linked results."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    EvaluationGroupRow,
    EvaluationResultRow,
    EvaluationRunRow,
    TrainingCheckpointRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
)
from coire_api.evaluation.groups import group_state
from coire_core.models.evaluation_links import EvaluationGroupLink


async def for_job(
    session: AsyncSession,
    job_id: str,
    *,
    adapter_id: uuid.UUID | None = None,
    checkpoint_id: uuid.UUID | None = None,
) -> list[EvaluationGroupLink]:
    statement = select(EvaluationGroupRow).where(EvaluationGroupRow.job_id == job_id)
    if adapter_id is not None:
        statement = statement.where(
            EvaluationGroupRow.subjects.contains([{"target": {"adapter_id": str(adapter_id)}}])
        )
    groups = list(
        (
            await session.scalars(
                statement.order_by(
                    EvaluationGroupRow.created_at.desc(), EvaluationGroupRow.id.desc()
                ).limit(100)
            )
        ).all()
    )
    trigger_query = select(TrainingEvaluationTriggerRow).where(
        TrainingEvaluationTriggerRow.job_id == job_id
    )
    if checkpoint_id is not None:
        trigger_query = trigger_query.where(
            TrainingEvaluationTriggerRow.checkpoint_id == checkpoint_id
        )
    triggers = list(
        (
            await session.scalars(
                trigger_query.order_by(
                    TrainingEvaluationTriggerRow.created_at.desc(),
                    TrainingEvaluationTriggerRow.id.desc(),
                ).limit(100)
            )
        ).all()
    )
    group_ids = [group.id for group in groups]
    runs = (
        list(
            (
                await session.scalars(
                    select(EvaluationRunRow)
                    .where(EvaluationRunRow.group_id.in_(group_ids))
                    .order_by(EvaluationRunRow.created_at, EvaluationRunRow.id)
                )
            ).all()
        )
        if group_ids
        else []
    )
    run_ids = [run.id for run in runs]
    results = (
        list(
            (
                await session.scalars(
                    select(EvaluationResultRow).where(EvaluationResultRow.run_id.in_(run_ids))
                )
            ).all()
        )
        if run_ids
        else []
    )
    result_by_run = {result.run_id: result.id for result in results}
    trigger_by_group = {trigger.group_id: trigger for trigger in triggers if trigger.group_id}
    checkpoint_ids = [trigger.checkpoint_id for trigger in triggers if trigger.checkpoint_id]
    checkpoints = (
        {
            row.id: row
            for row in (
                await session.scalars(
                    select(TrainingCheckpointRow).where(
                        TrainingCheckpointRow.id.in_(checkpoint_ids)
                    )
                )
            ).all()
        }
        if checkpoint_ids
        else {}
    )
    job = await session.get(TrainingJobRow, job_id) if triggers else None

    def boundary_fields(trigger: TrainingEvaluationTriggerRow | None) -> dict[str, object]:
        if trigger is None or trigger.boundary_kind != "checkpoint":
            return {}
        checkpoint = checkpoints.get(trigger.checkpoint_id) if trigger.checkpoint_id else None
        owner = (
            job.pause_origin
            if job and job.evaluation_pause_trigger_id == trigger.id
            else "released"
        )
        return {
            "attempt_id": checkpoint.attempt_id if checkpoint else None,
            "fence": trigger.fence,
            "trigger_phase": trigger.phase,
            "pause_owner": owner or "released",
            "resume_disposition": trigger.resume_disposition,
        }

    ordered = []
    for group in groups:
        children = [run for run in runs if run.group_id == group.id]
        trigger = trigger_by_group.get(group.id)
        link = EvaluationGroupLink.model_validate(
            {
                "trigger_id": trigger.id if trigger else None,
                "group_id": group.id,
                "origin": group.origin,
                "checkpoint_id": group.checkpoint_id,
                "completed_update": group.completed_update,
                "state": group_state(children),
                "run_ids": [run.id for run in children][:4],
                "result_ids": [
                    result_by_run[run.id] for run in children if run.id in result_by_run
                ][:4],
                **boundary_fields(trigger),
            }
        )
        ordered.append((group.created_at, link))
    for trigger in triggers:
        if trigger.group_id is None:
            link = EvaluationGroupLink.model_validate(
                {
                    "trigger_id": trigger.id,
                    "origin": f"training_{trigger.boundary_kind}",
                    "checkpoint_id": trigger.checkpoint_id,
                    "completed_update": trigger.completed_update,
                    "state": "failed" if trigger.phase == "complete" else "pending",
                    **boundary_fields(trigger),
                }
            )
            ordered.append((trigger.created_at, link))
    return [link for _, link in sorted(ordered, key=lambda item: item[0], reverse=True)[:100]]
