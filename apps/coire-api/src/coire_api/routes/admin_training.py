"""Authenticated training history and idempotent control routes."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select

from coire_api.db import (
    TrainingCheckpointRow,
    TrainingJobRow,
    TrainingMeasurementRow,
    TrainingProfileRow,
    session_scope,
)
from coire_api.evaluation.telemetry import mutation_scope
from coire_api.training.authorization import CurrentTrainingAdmin, authorize_live_training_action
from coire_api.training.checkpoints import checkpoint_detail
from coire_api.training.events import current_job, metric_page, replay_events
from coire_api.training.retention import retire_job
from coire_api.training.service import (
    control_training,
    decode_page_cursor,
    encode_page_cursor,
    job_detail,
    submit_training,
)
from coire_api.training.specs import parse_submission, resolve_submission, training_recipes
from coire_core.errors import (
    TrainingConflict,
    TrainingForbidden,
    TrainingNotFound,
    TrainingUnavailable,
    TrainingValidationError,
)
from coire_core.models.adapters import AdapterReceipt
from coire_core.models.training import (
    TERMINAL_TRAINING_STATES,
    CheckpointPage,
    CheckpointPromotionRequest,
    TrainingCommandReceipt,
    TrainingControlRequest,
    TrainingDeleteRequest,
    TrainingDeletionReceipt,
    TrainingEvent,
    TrainingJobDetail,
    TrainingJobPage,
    TrainingJobReceipt,
    TrainingJobState,
    TrainingMeasurementReceipt,
    TrainingMeasurementRequest,
    TrainingMeasurementResult,
    TrainingMetricPage,
    TrainingProfile,
    TrainingProfilePage,
    TrainingRecipePage,
    TrainingReplayPage,
    TrainingResetEvent,
    TrainingSpecV2,
    TrainingSpecV3,
    TrainingSubmission,
    TrainingValidation,
)

router = APIRouter(prefix="/api/v1/admin/training", tags=["admin:training"])


def enabled(request: Request) -> None:
    if not request.app.state.settings.training_enabled:
        raise TrainingUnavailable("Training is disabled")


def evaluated_submission(body: TrainingSubmission, request: Request) -> bool:
    spec = parse_submission(body, settings=request.app.state.settings).spec
    return isinstance(spec, (TrainingSpecV2, TrainingSpecV3)) and bool(spec.eval.suites)


@router.post("/validate", response_model=TrainingValidation)
async def validate_training(
    request: Request, body: TrainingSubmission, principal: CurrentTrainingAdmin
) -> TrainingValidation:
    enabled(request)
    scope = (
        mutation_scope(session_scope, principal, "evaluation.training.validate", "training")
        if evaluated_submission(body, request)
        else session_scope()
    )
    async with scope as session:
        await authorize_live_training_action(session, principal)
        return await resolve_submission(
            session, body, settings=request.app.state.settings, principal=principal
        )


@router.get("/recipes", response_model=TrainingRecipePage)
async def recipes(request: Request, principal: CurrentTrainingAdmin) -> TrainingRecipePage:
    enabled(request)
    return training_recipes()


@router.post("/jobs", response_model=TrainingJobReceipt, status_code=202)
async def submit_job(
    request: Request,
    body: TrainingSubmission,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> TrainingJobReceipt:
    enabled(request)
    scope = (
        mutation_scope(session_scope, principal, "evaluation.training.submit", "training")
        if evaluated_submission(body, request)
        else session_scope()
    )
    async with scope as session:
        return await submit_training(
            session, principal, body, idempotency_key, settings=request.app.state.settings
        )


@router.get("/jobs", response_model=TrainingJobPage)
async def list_jobs(
    request: Request,
    principal: CurrentTrainingAdmin,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    state: TrainingJobState | None = None,
) -> TrainingJobPage:
    async with session_scope() as session:
        statement = select(TrainingJobRow).where(TrainingJobRow.deleted_at.is_(None))
        if state is not None:
            statement = statement.where(TrainingJobRow.state == state.value)
        scope = "jobs:" + (state.value if state else "all")
        if cursor is not None:
            created_at, identity = decode_page_cursor(cursor, scope)
            import re

            if re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", identity) is None:
                raise TrainingValidationError("Invalid training job page cursor")
            statement = statement.where(
                or_(
                    TrainingJobRow.created_at < created_at,
                    and_(TrainingJobRow.created_at == created_at, TrainingJobRow.id < identity),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        TrainingJobRow.created_at.desc(), TrainingJobRow.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
        return TrainingJobPage(
            items=[await job_detail(session, row.id) for row in rows[:limit]],
            next_cursor=encode_page_cursor(scope, rows[limit - 1].created_at, rows[limit - 1].id)
            if len(rows) > limit
            else None,
        )


@router.get("/jobs/{job_id}", response_model=TrainingJobDetail)
async def get_job(
    request: Request, job_id: str, principal: CurrentTrainingAdmin
) -> TrainingJobDetail:
    async with session_scope() as session:
        return await job_detail(session, job_id)


@router.post("/jobs/{job_id}/{operation}", response_model=TrainingCommandReceipt, status_code=202)
async def control(
    request: Request,
    job_id: str,
    operation: Literal["pause", "resume", "cancel"],
    body: TrainingControlRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> TrainingCommandReceipt:
    if operation == "resume":
        enabled(request)
    async with session_scope() as session:
        return await control_training(session, principal, job_id, operation, body, idempotency_key)


@router.get("/jobs/{job_id}/events", response_model=TrainingReplayPage)
async def events(
    request: Request,
    job_id: str,
    principal: CurrentTrainingAdmin,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> TrainingReplayPage | StreamingResponse:
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit() or len(last_event_id) > 19:
            raise TrainingValidationError("Invalid Last-Event-ID")
        after = int(last_event_id)
    async with session_scope() as session:
        initial = await replay_events(session, job_id, after=after)
        snapshot = await current_job(session, job_id)
        terminal_head = (
            snapshot.next_event_sequence - 1 if snapshot.state in TERMINAL_TRAINING_STATES else None
        )
    if "text/event-stream" not in request.headers.get("accept", ""):
        return initial

    async def stream() -> AsyncIterator[str]:
        page, cursor, heartbeat = initial, after, 0
        while not await request.is_disconnected():
            if page.reset is not None:
                reset = TrainingEvent(
                    id=page.cursor,
                    job_id=job_id,
                    state_version=page.reset.version,
                    occurred_at=datetime.now(UTC),
                    kind="reset",
                    payload=TrainingResetEvent(snapshot=page.reset),
                )
                yield f"id: {reset.id}\nevent: reset\ndata: {reset.model_dump_json()}\n\n"
            for event in page.events:
                yield f"id: {event.id}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"
            cursor = page.cursor
            if (
                (terminal_head is not None and page.cursor >= terminal_head)
                or any(event.kind == "terminal" for event in page.events)
                or (page.reset is not None and page.reset.state in TERMINAL_TRAINING_STATES)
            ):
                return
            if heartbeat >= 15:
                yield ": heartbeat\n\n"
                heartbeat = 0
            if len(page.events) < 100:
                await asyncio.sleep(1)
                heartbeat += 1
            async with session_scope() as session:
                try:
                    await authorize_live_training_action(session, principal)
                    page = await replay_events(session, job_id, after=cursor)
                except (TrainingForbidden, TrainingNotFound):
                    return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/metrics", response_model=TrainingMetricPage)
async def metrics(
    request: Request,
    job_id: str,
    principal: CurrentTrainingAdmin,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 2000,
) -> TrainingMetricPage:
    async with session_scope() as session:
        return await metric_page(session, job_id, cursor=cursor, limit=limit)


@router.get("/jobs/{job_id}/checkpoints", response_model=CheckpointPage)
async def checkpoints(
    request: Request,
    job_id: str,
    principal: CurrentTrainingAdmin,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> CheckpointPage:
    async with session_scope() as session:
        await job_detail(session, job_id)
        statement = select(TrainingCheckpointRow).where(TrainingCheckpointRow.job_id == job_id)
        scope = "checkpoints:" + job_id
        if cursor is not None:
            created_at, identity = decode_page_cursor(cursor, scope)
            try:
                position = uuid.UUID(identity)
            except ValueError:
                raise TrainingValidationError("Invalid checkpoint page cursor") from None
            statement = statement.where(
                or_(
                    TrainingCheckpointRow.created_at > created_at,
                    and_(
                        TrainingCheckpointRow.created_at == created_at,
                        TrainingCheckpointRow.id > position,
                    ),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        TrainingCheckpointRow.created_at, TrainingCheckpointRow.id
                    ).limit(limit + 1)
                )
            ).all()
        )
        return CheckpointPage(
            items=[await checkpoint_detail(session, row) for row in rows[:limit]],
            next_cursor=encode_page_cursor(
                scope, rows[limit - 1].created_at, str(rows[limit - 1].id)
            )
            if len(rows) > limit
            else None,
        )


@router.delete("/jobs/{job_id}", response_model=TrainingDeletionReceipt, status_code=202)
async def delete_job(
    request: Request,
    job_id: str,
    body: TrainingDeleteRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> TrainingDeletionReceipt:
    async with session_scope() as session:
        return await retire_job(session, principal, job_id, body, idempotency_key)


@router.post("/checkpoints/{checkpoint_id}/promote", response_model=AdapterReceipt, status_code=202)
async def promote(
    request: Request,
    checkpoint_id: uuid.UUID,
    body: CheckpointPromotionRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> AdapterReceipt:
    enabled(request)
    from coire_api.training.adapters import enqueue_checkpoint_promotion

    async with session_scope() as session:
        return await enqueue_checkpoint_promotion(
            session,
            principal,
            checkpoint_id,
            body,
            idempotency_key,
            extraction_available=True,
        )


@router.post("/measurements", response_model=TrainingMeasurementReceipt, status_code=202)
async def measure(
    request: Request,
    body: TrainingMeasurementRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> TrainingMeasurementReceipt:
    enabled(request)
    from coire_api.training.measurements import submit_measurement

    async with session_scope() as session:
        return await submit_measurement(
            session,
            principal,
            body,
            idempotency_key,
            settings=request.app.state.settings,
            prompts=body.prompts,
        )


@router.get("/measurements/{measurement_id}", response_model=TrainingMeasurementResult)
async def measurement(
    request: Request, measurement_id: uuid.UUID, principal: CurrentTrainingAdmin
) -> TrainingMeasurementResult:
    async with session_scope() as session:
        row = await session.get(TrainingMeasurementRow, measurement_id)
        if row is None:
            raise TrainingNotFound()
        if row.report is not None:
            result = TrainingMeasurementResult.model_validate(row.report)
            if (
                result.id != row.id
                or result.state != row.state
                or result.request.model_dump(mode="json") != row.request
            ):
                raise TrainingConflict("Measurement report identity changed")
            return result
        if row.state == "succeeded":
            raise TrainingConflict("Successful measurement has no recorded evidence")
        return TrainingMeasurementResult.model_validate(
            {"id": row.id, "request": row.request, "state": row.state, "created_at": row.created_at}
        )


@router.get("/profiles", response_model=TrainingProfilePage)
async def profiles(
    request: Request,
    principal: CurrentTrainingAdmin,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TrainingProfilePage:
    async with session_scope() as session:
        statement = select(TrainingProfileRow)
        if cursor is not None:
            created_at, identity = decode_page_cursor(cursor, "profiles")
            try:
                position = uuid.UUID(identity)
            except ValueError:
                raise TrainingValidationError("Invalid profile page cursor") from None
            statement = statement.where(
                or_(
                    TrainingProfileRow.created_at > created_at,
                    and_(
                        TrainingProfileRow.created_at == created_at,
                        TrainingProfileRow.id > position,
                    ),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(TrainingProfileRow.created_at, TrainingProfileRow.id).limit(
                        limit + 1
                    )
                )
            ).all()
        )
        return TrainingProfilePage(
            items=[
                TrainingProfile.model_validate(
                    {
                        **row.profile,
                        "id": row.id,
                        "report_sha256": row.report_sha256,
                        "expires_at": row.valid_until,
                        "invalidated_reason": row.invalidated_reason,
                    }
                )
                for row in rows[:limit]
            ],
            next_cursor=encode_page_cursor(
                "profiles", rows[limit - 1].created_at, str(rows[limit - 1].id)
            )
            if len(rows) > limit
            else None,
        )
