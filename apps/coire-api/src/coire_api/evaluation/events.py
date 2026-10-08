"""Transactional bounded content-free evaluation event replay."""

import logging
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import EvaluationEventRow, EvaluationRunRow
from coire_core.errors import EvaluationConflict, EvaluationNotFound
from coire_core.models.evaluation import EvaluationEvent

tracer = trace.get_tracer("coire.api.evaluation")
logger = logging.getLogger(__name__)
events_total = metrics.get_meter("coire.api.evaluation").create_counter(
    "coire_evaluation_events_total"
)
MAX_EVENTS = 1000


async def current_run(session: AsyncSession, run_id: str, *, lock: bool = True) -> EvaluationRunRow:
    row = await session.get(EvaluationRunRow, run_id, populate_existing=True, with_for_update=lock)
    if row is None:
        raise EvaluationNotFound()
    return row


async def append(
    session: AsyncSession, run: EvaluationRunRow, *, kind: str = "state", completed_cases: int = 0
) -> EvaluationEvent:
    with tracer.start_as_current_span(
        "coire.api.evaluation.event.append", record_exception=False, set_status_on_exception=False
    ):
        event = EvaluationEvent.model_validate(
            {
                "sequence": run.next_event_sequence,
                "evaluation_id": run.id,
                "kind": kind,
                "state": run.state,
                "phase": run.phase,
                "version": run.version,
                "created_at": datetime.now(UTC),
                "reason": run.safe_failure_code,
                "completed_cases": completed_cases,
            }
        )
        session.add(
            EvaluationEventRow(
                run_id=run.id,
                sequence=event.sequence,
                payload=event.model_dump(mode="json"),
                created_at=event.created_at,
            )
        )
        run.next_event_sequence += 1
        await session.flush()
        await session.execute(
            delete(EvaluationEventRow).where(
                EvaluationEventRow.run_id == run.id,
                EvaluationEventRow.sequence <= event.sequence - MAX_EVENTS,
            )
        )
        events_total.add(1, {"kind": event.kind, "state": event.state.value})
        from coire_api.evaluation.groups import append as append_group

        await append_group(session, run)
        logger.info(
            "evaluation state recorded",
            extra={
                "run_id": run.id,
                "user_id": str(run.owner_user_id),
                "group_id": run.group_id,
                "phase": run.phase,
                "state": run.state,
                "safe_reason": run.safe_failure_code,
            },
        )
        return event


async def replay(
    session: AsyncSession, run_id: str, *, after: int = 0
) -> tuple[list[EvaluationEvent], bool]:
    run = await current_run(session, run_id, lock=False)
    if after < 0 or after >= run.next_event_sequence:
        raise EvaluationConflict("Evaluation event cursor is invalid")
    rows = (
        await session.scalars(
            select(EvaluationEventRow)
            .where(EvaluationEventRow.run_id == run_id, EvaluationEventRow.sequence > after)
            .order_by(EvaluationEventRow.sequence)
            .limit(MAX_EVENTS)
        )
    ).all()
    events = [EvaluationEvent.model_validate(row.payload) for row in rows]
    return events, bool(events and events[0].sequence != after + 1)
