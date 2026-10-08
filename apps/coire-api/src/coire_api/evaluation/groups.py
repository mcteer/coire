"""Independent bounded durable group history, including after training terminates."""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import EvaluationGroupEventRow, EvaluationGroupRow, EvaluationRunRow
from coire_core.errors import EvaluationConflict, EvaluationNotFound
from coire_core.models.evaluation import (
    TERMINAL_EVALUATION_STATES,
    EvaluationGroupDetail,
    EvaluationGroupEvent,
    EvaluationGroupReplayPage,
    EvaluationReceipt,
    EvaluationState,
)

MAX_EVENTS = 1000
type GroupState = Literal["pending", "running", "succeeded", "failed", "cancelled"]


def group_state(rows: list[EvaluationRunRow]) -> GroupState:
    states = {row.state for row in rows}
    if not rows or states == {"queued"}:
        return "pending"
    if any(row.state not in TERMINAL_EVALUATION_STATES for row in rows):
        return "running"
    return (
        "succeeded"
        if states == {"succeeded"}
        else "cancelled"
        if states == {"cancelled"}
        else "failed"
    )


async def runs(session: AsyncSession, group_id: str) -> list[EvaluationRunRow]:
    return list(
        (
            await session.scalars(
                select(EvaluationRunRow)
                .where(EvaluationRunRow.group_id == group_id)
                .order_by(EvaluationRunRow.created_at, EvaluationRunRow.id)
            )
        ).all()
    )


async def detail(session: AsyncSession, group_id: str) -> EvaluationGroupDetail:
    from coire_api.evaluation.service import detail as run_detail

    group = await session.get(EvaluationGroupRow, group_id)
    if group is None:
        raise EvaluationNotFound()
    rows = await runs(session, group_id)
    return EvaluationGroupDetail.model_validate(
        {
            "id": group.id,
            "origin": group.origin,
            "training_job_id": group.job_id,
            "checkpoint_id": group.checkpoint_id,
            "update": group.completed_update,
            "runs": [await run_detail(session, row.id) for row in rows],
            "state": group_state(rows),
            "created_at": group.created_at,
        }
    )


async def append(session: AsyncSession, run: EvaluationRunRow) -> None:
    group = await session.get(
        EvaluationGroupRow, run.group_id, with_for_update=True, populate_existing=True
    )
    if group is None:
        raise EvaluationNotFound()
    state = group_state(await runs(session, group.id))
    event = EvaluationGroupEvent(
        sequence=group.next_event_sequence,
        group_id=group.id,
        kind="terminal" if state in {"succeeded", "failed", "cancelled"} else "state",
        state=state,
        created_at=datetime.now(UTC),
        run=EvaluationReceipt(
            id=run.id,
            group_id=group.id,
            state=EvaluationState(run.state),
            version=run.version,
            events_path=f"/api/v1/admin/evaluations/{run.id}/events",
        ),
    )
    group.next_event_sequence += 1
    session.add(
        EvaluationGroupEventRow(
            group_id=group.id,
            sequence=event.sequence,
            payload=event.model_dump(mode="json"),
            created_at=event.created_at,
        )
    )
    await session.flush()
    await session.execute(
        delete(EvaluationGroupEventRow).where(
            EvaluationGroupEventRow.group_id == group.id,
            EvaluationGroupEventRow.sequence <= event.sequence - MAX_EVENTS,
        )
    )


async def replay(
    session: AsyncSession, group_id: str, *, after: int = 0
) -> EvaluationGroupReplayPage:
    group = await session.get(EvaluationGroupRow, group_id, populate_existing=True)
    if group is None:
        raise EvaluationNotFound()
    if after < 0 or after >= group.next_event_sequence:
        raise EvaluationConflict("Evaluation group cursor is invalid")
    rows = (
        await session.scalars(
            select(EvaluationGroupEventRow)
            .where(
                EvaluationGroupEventRow.group_id == group_id,
                EvaluationGroupEventRow.sequence > after,
            )
            .order_by(EvaluationGroupEventRow.sequence)
            .limit(MAX_EVENTS)
        )
    ).all()
    values = [EvaluationGroupEvent.model_validate(row.payload) for row in rows]
    reset = bool(values and values[0].sequence != after + 1)
    return EvaluationGroupReplayPage(
        events=[] if reset else values,
        cursor=group.next_event_sequence - 1 if reset else values[-1].sequence if values else after,
        reset=await detail(session, group_id) if reset else None,
    )
