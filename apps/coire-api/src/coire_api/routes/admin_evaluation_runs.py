"""Durable Studio evaluation submission, history and bounded private receipts."""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import and_, or_, select

from coire_api.db import (
    EvaluationEvidenceRow,
    EvaluationGroupRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    session_scope,
)
from coire_api.evaluation import events, groups, idempotency, measurements, service
from coire_api.evaluation.authorization import (
    CurrentEvaluationAdmin,
    CurrentEvaluationReader,
    authorize_evaluation_read,
    authorize_live_evaluation_action,
)
from coire_api.evaluation.evidence import EvidenceStore
from coire_api.evaluation.telemetry import mutation_scope
from coire_api.nodes_client import NodeClient
from coire_api.routes.admin_evaluation_suites import Key
from coire_api.training.service import decode_page_cursor, encode_page_cursor
from coire_core.errors import (
    EvaluationConflict,
    EvaluationEvidenceGone,
    EvaluationForbidden,
    EvaluationNotFound,
    EvaluationValidationError,
    TrainingValidationError,
)
from coire_core.models.evaluation import (
    TERMINAL_EVALUATION_STATES,
    EvaluationComparison,
    EvaluationControl,
    EvaluationEvent,
    EvaluationGroupDetail,
    EvaluationGroupEvent,
    EvaluationGroupReplayPage,
    EvaluationMeasurement,
    EvaluationMeasurementDetail,
    EvaluationMeasurementRequest,
    EvaluationReceipt,
    EvaluationReplayPage,
    EvaluationRunDetail,
    EvaluationRunPage,
    EvaluationState,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkerResult,
)
from coire_core.models.training_types import TrainingId

router = APIRouter(prefix="/api/v1/admin", tags=["admin:evaluations"])


@router.post("/evaluations", response_model=EvaluationReceipt, status_code=202)
async def submit_run(
    request: Request,
    body: EvaluationSubmission,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationReceipt:
    async with (
        mutation_scope(session_scope, principal, "evaluation.submit", "submission") as session,
        NodeClient(request.app.state.settings) as client,
    ):
        return await service.submit(
            session,
            principal,
            body,
            idempotency_key=idempotency_key,
            settings=request.app.state.settings,
            client=client,
        )


@router.get("/evaluations", response_model=EvaluationRunPage)
async def list_runs(
    principal: CurrentEvaluationReader,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    state: EvaluationState | None = None,
    model_id: uuid.UUID | None = None,
    variant_id: uuid.UUID | None = None,
    adapter_id: uuid.UUID | None = None,
    training_job_id: TrainingId | None = None,
    group_id: TrainingId | None = None,
) -> EvaluationRunPage:
    scope = f"evaluations:{state}:{model_id}:{variant_id}:{adapter_id}:{training_job_id}:{group_id}"
    async with session_scope() as session:
        statement = select(EvaluationRunRow).join(
            EvaluationGroupRow, EvaluationGroupRow.id == EvaluationRunRow.group_id
        )
        if state is not None:
            statement = statement.where(EvaluationRunRow.state == state.value)
        if group_id is not None:
            statement = statement.where(EvaluationRunRow.group_id == group_id)
        if training_job_id is not None:
            statement = statement.where(EvaluationGroupRow.job_id == training_job_id)
        exact = {
            key: str(value)
            for key, value in (
                ("model_id", model_id),
                ("variant_id", variant_id),
                ("adapter_id", adapter_id),
            )
            if value is not None
        }
        if exact:
            statement = statement.where(EvaluationRunRow.subjects.contains([{"target": exact}]))
        if cursor is not None:
            try:
                created, identity = decode_page_cursor(cursor, scope)
                if re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", identity) is None:
                    raise ValueError()
            except (TrainingValidationError, ValueError):
                raise EvaluationValidationError("Invalid evaluation page cursor") from None
            statement = statement.where(
                or_(
                    EvaluationRunRow.created_at < created,
                    and_(EvaluationRunRow.created_at == created, EvaluationRunRow.id < identity),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        EvaluationRunRow.created_at.desc(), EvaluationRunRow.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
        return EvaluationRunPage(
            items=[await service.detail(session, row.id) for row in rows[:limit]],
            next_cursor=encode_page_cursor(scope, rows[limit - 1].created_at, rows[limit - 1].id)
            if len(rows) > limit
            else None,
        )


@router.get("/evaluations/{run_id}", response_model=EvaluationRunDetail)
async def get_run(run_id: TrainingId, principal: CurrentEvaluationReader) -> EvaluationRunDetail:
    async with session_scope() as session:
        return await service.detail(session, run_id)


@router.post("/evaluations/{run_id}/cancel", response_model=EvaluationReceipt, status_code=202)
async def cancel_run(
    run_id: TrainingId,
    body: EvaluationControl,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationReceipt:
    async with mutation_scope(session_scope, principal, "evaluation.cancel", run_id) as session:
        owner = await authorize_live_evaluation_action(session, principal)
        operation = f"evaluation.cancel:{run_id}"
        previous = await idempotency.replay(session, owner, operation, idempotency_key, body)
        if previous is not None:
            return EvaluationReceipt.model_validate(previous.response)
        result = await service.cancel(session, principal, run_id, body)
        await idempotency.record(session, owner, operation, idempotency_key, body, result)
        return result


@router.post("/evaluations/{run_id}/rerun", response_model=EvaluationReceipt, status_code=202)
async def rerun(
    request: Request,
    run_id: TrainingId,
    body: EvaluationControl,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationReceipt:
    async with (
        mutation_scope(session_scope, principal, "evaluation.rerun", run_id) as session,
        NodeClient(request.app.state.settings) as client,
    ):
        owner = await authorize_live_evaluation_action(session, principal)
        operation = f"evaluation.rerun:{run_id}"
        previous = await idempotency.replay(session, owner, operation, idempotency_key, body)
        if previous is not None:
            return EvaluationReceipt.model_validate(previous.response)
        original = await service.detail(session, run_id)
        if original.version != body.expected_version:
            raise EvaluationConflict("Evaluation rerun version is stale")
        submission = EvaluationSubmission(
            suite_id=original.suite.suite_id,
            suite_version=original.suite.version,
            subjects=[
                EvaluationSubject(
                    model_id=item.target.model_id,
                    variant_id=item.target.variant_id,
                    adapter_id=item.target.adapter_id,
                )
                for item in original.subjects
            ],
            training_job_id=original.training_job_id,
        )
        result = await service.submit(
            session,
            principal,
            submission,
            idempotency_key=idempotency_key,
            settings=request.app.state.settings,
            client=client,
            source_run_id=run_id,
        )
        await idempotency.record(session, owner, operation, idempotency_key, body, result)
        return result


@router.get(
    "/evaluations/{run_id}/evidence/{evidence_id}",
    response_class=Response,
    response_model=EvaluationWorkerResult,
)
async def get_evidence(
    request: Request,
    run_id: TrainingId,
    evidence_id: uuid.UUID,
    principal: CurrentEvaluationReader,
) -> Response:
    async with session_scope() as session:
        await events.current_run(session, run_id, lock=False)
        row = await session.get(EvaluationEvidenceRow, evidence_id)
        if row is None or row.run_id != run_id:
            raise EvaluationNotFound()
        if row.availability != "present" or row.expires_at <= datetime.now(UTC):
            raise EvaluationEvidenceGone()
        if row.storage_key != str(row.id):
            raise EvaluationValidationError("Evidence storage receipt is invalid")
        content = await EvidenceStore(request.app.state.settings).read(row.id, row.sha256)
        return Response(
            content, media_type="application/json", headers={"Cache-Control": "no-store"}
        )


@router.get("/evaluations/{run_id}/events", response_model=EvaluationReplayPage)
async def replay_events(
    request: Request,
    run_id: TrainingId,
    principal: CurrentEvaluationReader,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(max_length=19)] = None,
) -> EvaluationReplayPage | StreamingResponse:
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit():
            raise EvaluationValidationError("Invalid Last-Event-ID")
        after = int(last_event_id)

    async def page(cursor: int) -> EvaluationReplayPage:
        async with session_scope() as session:
            # Streaming credentials are rechecked on every poll, including before initial data.
            await authorize_evaluation_read(
                session,
                principal,
                legacy_enabled=request.app.state.settings.identity_legacy_admin_enabled,
            )
            values, reset = await events.replay(session, run_id, after=cursor)
            run = await events.current_run(session, run_id, lock=False)
            return EvaluationReplayPage(
                events=[] if reset else values,
                cursor=run.next_event_sequence - 1
                if reset
                else values[-1].sequence
                if values
                else cursor,
                reset=await service.detail(session, run_id) if reset else None,
            )

    initial = await page(after)
    if "text/event-stream" not in request.headers.get("accept", ""):
        return initial

    async def stream() -> AsyncIterator[str]:
        current = initial
        while not await request.is_disconnected():
            if current.reset is not None:
                snapshot = current.reset
                reset = EvaluationEvent(
                    sequence=max(1, current.cursor),
                    evaluation_id=run_id,
                    kind="reset",
                    state=snapshot.state,
                    version=snapshot.version,
                    created_at=datetime.now(UTC),
                    snapshot=snapshot,
                )
                yield f"id: {current.cursor}\nevent: reset\ndata: {reset.model_dump_json()}\n\n"
            for value in current.events:
                yield f"id: {value.sequence}\nevent: {value.kind}\ndata: {value.model_dump_json()}\n\n"
            if any(value.kind == "terminal" for value in current.events) or (
                current.reset is not None and current.reset.state in TERMINAL_EVALUATION_STATES
            ):
                return
            async with session_scope() as session:
                row = await events.current_run(session, run_id, lock=False)
                if (
                    row.state in TERMINAL_EVALUATION_STATES
                    and current.cursor >= row.next_event_sequence - 1
                ):
                    return
            await asyncio.sleep(1)
            try:
                current = await page(current.cursor)
            except (EvaluationForbidden, EvaluationNotFound):
                return
            yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/evaluation-groups/{group_id}", response_model=EvaluationGroupDetail)
async def get_group(
    group_id: TrainingId, principal: CurrentEvaluationReader
) -> EvaluationGroupDetail:
    async with session_scope() as session:
        return await groups.detail(session, group_id)


@router.get("/evaluation-groups/{group_id}/events", response_model=EvaluationGroupReplayPage)
async def group_events(
    request: Request,
    group_id: TrainingId,
    principal: CurrentEvaluationReader,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(max_length=19)] = None,
) -> EvaluationGroupReplayPage | StreamingResponse:
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit():
            raise EvaluationValidationError("Invalid Last-Event-ID")
        after = int(last_event_id)

    async def page(cursor: int) -> EvaluationGroupReplayPage:
        async with session_scope() as session:
            await authorize_evaluation_read(
                session,
                principal,
                legacy_enabled=request.app.state.settings.identity_legacy_admin_enabled,
            )
            return await groups.replay(session, group_id, after=cursor)

    initial = await page(after)
    if "text/event-stream" not in request.headers.get("accept", ""):
        return initial

    async def stream() -> AsyncIterator[str]:
        current = initial
        while not await request.is_disconnected():
            if current.reset is not None:
                snapshot = current.reset
                reset = EvaluationGroupEvent(
                    sequence=max(1, current.cursor),
                    group_id=group_id,
                    kind="reset",
                    state=snapshot.state,
                    created_at=datetime.now(UTC),
                    snapshot=snapshot,
                )
                yield f"id: {current.cursor}\nevent: reset\ndata: {reset.model_dump_json()}\n\n"
            for value in current.events:
                yield f"id: {value.sequence}\nevent: {value.kind}\ndata: {value.model_dump_json()}\n\n"
            if any(value.kind == "terminal" for value in current.events) or (
                current.reset is not None
                and current.reset.state in {"succeeded", "failed", "cancelled"}
            ):
                return
            async with session_scope() as session:
                snapshot = await groups.detail(session, group_id)
                group = await session.get(EvaluationGroupRow, group_id)
                if (
                    group is not None
                    and snapshot.state in {"succeeded", "failed", "cancelled"}
                    and current.cursor >= group.next_event_sequence - 1
                ):
                    return
            await asyncio.sleep(1)
            try:
                current = await page(current.cursor)
            except (EvaluationForbidden, EvaluationNotFound):
                return
            yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/evaluation-measurements", response_model=EvaluationMeasurement, status_code=202)
async def measure(
    request: Request,
    body: EvaluationMeasurementRequest,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationMeasurement:
    async with (
        mutation_scope(
            session_scope, principal, "evaluation.measurement.submit", "measurement"
        ) as session,
        NodeClient(request.app.state.settings) as client,
    ):
        return await measurements.submit_measurement(
            session,
            principal,
            body,
            key=idempotency_key,
            settings=request.app.state.settings,
            client=client,
        )


@router.get("/evaluation-measurements/{measurement_id}", response_model=EvaluationMeasurementDetail)
async def get_measurement(
    measurement_id: uuid.UUID, principal: CurrentEvaluationReader
) -> EvaluationMeasurementDetail:
    async with session_scope() as session:
        row = await session.get(EvaluationMeasurementRow, measurement_id)
        if row is None:
            raise EvaluationNotFound()
        return await measurements.detail(session, row)


@router.post(
    "/evaluation-measurements/{measurement_id}/cancel",
    response_model=EvaluationMeasurement,
    status_code=202,
)
async def cancel_measurement(
    measurement_id: uuid.UUID,
    body: EvaluationControl,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationMeasurement:
    async with mutation_scope(
        session_scope, principal, "evaluation.measurement.cancel", str(measurement_id)
    ) as session:
        owner = await authorize_live_evaluation_action(session, principal)
        operation = f"measurement.cancel:{measurement_id}"
        previous = await idempotency.replay(session, owner, operation, idempotency_key, body)
        if previous is not None:
            return EvaluationMeasurement.model_validate(previous.response)
        result = await measurements.cancel_measurement(session, principal, measurement_id, body)
        await idempotency.record(session, owner, operation, idempotency_key, body, result)
        return result


@router.get("/evaluation-comparisons", response_model=EvaluationComparison)
async def compare_results(
    request: Request,
    principal: CurrentEvaluationReader,
    left_result_id: TrainingId | uuid.UUID,
    right_result_id: TrainingId | uuid.UUID,
    left_subject: Annotated[int, Query(ge=0, le=1)] = 0,
    right_subject: Annotated[int, Query(ge=0, le=1)] = 0,
) -> EvaluationComparison:
    from coire_api.evaluation.comparison import compare_stored_results

    async with session_scope() as session:
        await authorize_evaluation_read(
            session,
            principal,
            legacy_enabled=request.app.state.settings.identity_legacy_admin_enabled,
        )
        return await compare_stored_results(
            session,
            str(left_result_id),
            str(right_result_id),
            left_subject=left_subject,
            right_subject=right_subject,
        )
