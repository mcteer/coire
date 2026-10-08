"""Authenticated immutable suite registry; admission configuration is explicit."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy import and_, or_, select

from coire_api.db import EvaluationSuiteRow, session_scope
from coire_api.evaluation import catalog, idempotency
from coire_api.evaluation.authorization import (
    CurrentEvaluationAdmin,
    CurrentEvaluationReader,
    authorize_live_evaluation_action,
    resolve_evaluation_target,
)
from coire_api.evaluation.telemetry import mutation_scope
from coire_api.nodes_client import NodeClient
from coire_api.training.service import decode_page_cursor, encode_page_cursor
from coire_core.errors import EvaluationValidationError, TrainingValidationError
from coire_core.evaluation_suites import templates
from coire_core.models.evaluation import (
    EvaluationControl,
    EvaluationSuite,
    EvaluationSuitePage,
    EvaluationSuiteRegistration,
    EvaluationTemplatePage,
)
from coire_core.models.training_types import AdapterSlug

router = APIRouter(prefix="/api/v1/admin", tags=["admin:evaluations"])
Key = Annotated[str, Header(min_length=1, max_length=128, alias="Idempotency-Key")]


@router.get("/evaluation-suite-templates", response_model=EvaluationTemplatePage)
async def list_templates(principal: CurrentEvaluationReader) -> EvaluationTemplatePage:
    return EvaluationTemplatePage(items=templates())


@router.post("/evaluation-suites", response_model=EvaluationSuite, status_code=201)
async def register_suite(
    request: Request,
    body: EvaluationSuiteRegistration,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationSuite:
    async with mutation_scope(
        session_scope, principal, "evaluation.suite.register", body.suite_id
    ) as session:
        owner = await authorize_live_evaluation_action(session, principal)
        previous = await idempotency.replay(session, owner, "suite.register", idempotency_key, body)
        if previous is not None:
            return EvaluationSuite.model_validate(previous.response)
        judge = None
        if body.judge is not None:
            async with NodeClient(request.app.state.settings) as client:
                judge = await resolve_evaluation_target(session, principal, body.judge, client)
        result = await catalog.register(session, principal, body, judge=judge)
        await idempotency.record(session, owner, "suite.register", idempotency_key, body, result)
        return result


@router.get("/evaluation-suites", response_model=EvaluationSuitePage)
async def list_suites(
    principal: CurrentEvaluationReader,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    retired: bool | None = None,
) -> EvaluationSuitePage:
    scope = f"evaluation-suites:{retired}"
    async with session_scope() as session:
        statement = select(EvaluationSuiteRow)
        if retired is not None:
            statement = statement.where(EvaluationSuiteRow.retired == retired)
        if cursor is not None:
            try:
                created, identity = decode_page_cursor(cursor, scope)
                row_id = uuid.UUID(identity)
            except (TrainingValidationError, ValueError):
                raise EvaluationValidationError("Invalid evaluation suite cursor") from None
            statement = statement.where(
                or_(
                    EvaluationSuiteRow.created_at < created,
                    and_(EvaluationSuiteRow.created_at == created, EvaluationSuiteRow.id < row_id),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        EvaluationSuiteRow.created_at.desc(), EvaluationSuiteRow.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
        return EvaluationSuitePage(
            items=[catalog.project(row) for row in rows[:limit]],
            next_cursor=encode_page_cursor(
                scope, rows[limit - 1].created_at, str(rows[limit - 1].id)
            )
            if len(rows) > limit
            else None,
        )


@router.get("/evaluation-suites/{suite_id}/versions/{version}", response_model=EvaluationSuite)
async def get_suite(
    suite_id: AdapterSlug,
    version: Annotated[int, Path(ge=1, le=2**31 - 1)],
    principal: CurrentEvaluationReader,
) -> EvaluationSuite:
    async with session_scope() as session:
        return catalog.project(await catalog.get_row(session, suite_id, version))


@router.post(
    "/evaluation-suites/{suite_id}/versions/{version}/retire", response_model=EvaluationSuite
)
async def retire_suite(
    suite_id: AdapterSlug,
    version: Annotated[int, Path(ge=1, le=2**31 - 1)],
    body: EvaluationControl,
    principal: CurrentEvaluationAdmin,
    idempotency_key: Key,
) -> EvaluationSuite:
    async with mutation_scope(
        session_scope, principal, "evaluation.suite.retire", suite_id
    ) as session:
        owner = await authorize_live_evaluation_action(session, principal)
        operation = f"suite.retire:{suite_id}:{version}"
        previous = await idempotency.replay(session, owner, operation, idempotency_key, body)
        if previous is not None:
            return EvaluationSuite.model_validate(previous.response)
        result = await catalog.retire(
            session, principal, suite_id, version, expected_version=body.expected_version
        )
        await idempotency.record(session, owner, operation, idempotency_key, body, result)
        return result
