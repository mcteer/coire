"""Authenticated private feedback export history and mutation routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from sqlalchemy import select

from coire_api.db import PreferenceExportRow
from coire_api.deps import SessionDep
from coire_api.feedback.eligibility import authorize_admin
from coire_api.feedback.exports import cancel_export, export_detail, export_row, submit_export
from coire_api.feedback.telemetry import mutation_scope
from coire_api.routes.chat_feedback import FeedbackSettingsDep
from coire_api.training.authorization import CurrentTrainingAdmin
from coire_core.models.feedback import (
    AdminPairJudgement,
    FeedbackReviewDetail,
    FeedbackReviewPage,
    FeedbackReviewReceipt,
    PreferenceExportCancel,
    PreferenceExportCreate,
    PreferenceExportDetail,
    PreferenceExportPage,
    PreferenceExportReceipt,
)
from coire_core.models.training_types import TrainingId

router = APIRouter(prefix="/api/v1/admin/feedback", tags=["admin:feedback"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


@router.get("/comparisons", response_model=FeedbackReviewPage)
async def get_review_queue(
    principal: CurrentTrainingAdmin,
    session: SessionDep,
    state: Literal["unreviewed", "reviewed", "skipped"] = "unreviewed",
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> FeedbackReviewPage:
    from coire_api.feedback.review import review_page

    return await review_page(session, principal, state=state, cursor=cursor, limit=limit)


@router.get("/comparisons/{pair_id}", response_model=FeedbackReviewDetail)
async def get_review_pair(
    pair_id: TrainingId, principal: CurrentTrainingAdmin, session: SessionDep
) -> FeedbackReviewDetail:
    from coire_api.feedback.review import review_detail

    return await review_detail(session, principal, pair_id)


@router.put("/comparisons/{pair_id}/judgement", response_model=FeedbackReviewReceipt)
async def put_review_judgement(
    pair_id: TrainingId,
    body: AdminPairJudgement,
    principal: CurrentTrainingAdmin,
    idempotency_key: IdempotencyKey,
) -> FeedbackReviewReceipt:
    from coire_api.feedback.review import judge_pair

    async with mutation_scope(principal, "review_judgement") as session:
        result = await judge_pair(session, principal, pair_id, body, idempotency_key)
        await session.commit()
        return result


@router.post("/exports", status_code=202, response_model=PreferenceExportReceipt)
async def post_export(
    body: PreferenceExportCreate,
    principal: CurrentTrainingAdmin,
    settings: FeedbackSettingsDep,
    idempotency_key: IdempotencyKey,
) -> PreferenceExportReceipt:
    async with mutation_scope(principal, "export_create") as session:
        result = await submit_export(session, principal, body, idempotency_key, settings)
        await session.commit()
        return result


@router.get("/exports", response_model=PreferenceExportPage)
async def get_exports(
    principal: CurrentTrainingAdmin,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")] = None,
) -> PreferenceExportPage:
    await authorize_admin(session, principal)
    statement = select(PreferenceExportRow).order_by(PreferenceExportRow.id.desc()).limit(limit + 1)
    if cursor is not None:
        statement = statement.where(PreferenceExportRow.id < cursor)
    rows = list(await session.scalars(statement))
    return PreferenceExportPage(
        items=[export_detail(row) for row in rows[:limit]],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get("/exports/{export_id}", response_model=PreferenceExportDetail)
async def get_export(
    export_id: TrainingId,
    principal: CurrentTrainingAdmin,
    session: SessionDep,
) -> PreferenceExportDetail:
    return export_detail(await export_row(session, principal, export_id))


@router.post("/exports/{export_id}/cancel", response_model=PreferenceExportReceipt)
async def post_export_cancel(
    export_id: TrainingId,
    body: PreferenceExportCancel,
    principal: CurrentTrainingAdmin,
    idempotency_key: IdempotencyKey,
) -> PreferenceExportReceipt:
    async with mutation_scope(principal, "export_cancel") as session:
        result = await cancel_export(session, principal, export_id, body, idempotency_key)
        await session.commit()
        return result
