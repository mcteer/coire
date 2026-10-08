"""Private admin projections and transactional adapter curation."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import and_, or_, select

from coire_api.db import TrainingAdapterRow, session_scope
from coire_api.routes.admin_training import enabled
from coire_api.training.adapters import adapter_evaluation_detail, curate_adapter
from coire_api.training.authorization import CurrentTrainingAdmin
from coire_api.training.service import decode_page_cursor, encode_page_cursor
from coire_core.errors import TrainingNotFound, TrainingValidationError
from coire_core.models.adapters import (
    AdapterCurationRequest,
    AdapterDetail,
    AdapterPage,
    AdapterReceipt,
    AdapterRetireRequest,
)

router = APIRouter(prefix="/api/v1/admin/adapters", tags=["admin:adapters"])


@router.get("", response_model=AdapterPage)
async def list_adapters(
    request: Request,
    principal: CurrentTrainingAdmin,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AdapterPage:
    enabled(request)
    async with session_scope() as session:
        statement = select(TrainingAdapterRow).where(TrainingAdapterRow.purpose != "evaluation")
        if cursor is not None:
            created_at, identity = decode_page_cursor(cursor, "adapters")
            try:
                position = uuid.UUID(identity)
            except ValueError:
                raise TrainingValidationError("Invalid adapter page cursor") from None
            statement = statement.where(
                or_(
                    TrainingAdapterRow.created_at > created_at,
                    and_(
                        TrainingAdapterRow.created_at == created_at,
                        TrainingAdapterRow.id > position,
                    ),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(TrainingAdapterRow.created_at, TrainingAdapterRow.id).limit(
                        limit + 1
                    )
                )
            ).all()
        )
        return AdapterPage(
            items=[await adapter_evaluation_detail(session, row) for row in rows[:limit]],
            next_cursor=encode_page_cursor(
                "adapters", rows[limit - 1].created_at, str(rows[limit - 1].id)
            )
            if len(rows) > limit
            else None,
        )


@router.get("/{adapter_id}", response_model=AdapterDetail)
async def get_adapter(
    request: Request, adapter_id: uuid.UUID, principal: CurrentTrainingAdmin
) -> AdapterDetail:
    enabled(request)
    async with session_scope() as session:
        row = await session.get(TrainingAdapterRow, adapter_id)
        if row is None or row.purpose == "evaluation":
            raise TrainingNotFound()
        return await adapter_evaluation_detail(session, row)


@router.patch("/{adapter_id}", response_model=AdapterDetail)
async def curate(
    request: Request,
    adapter_id: uuid.UUID,
    body: AdapterCurationRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> AdapterDetail:
    enabled(request)
    async with session_scope() as session:
        return await curate_adapter(session, principal, adapter_id, body, idempotency_key)


@router.delete("/{adapter_id}", response_model=AdapterReceipt)
async def retire(
    request: Request,
    adapter_id: uuid.UUID,
    body: AdapterRetireRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> AdapterReceipt:
    enabled(request)
    async with session_scope() as session:
        detail = await curate_adapter(session, principal, adapter_id, body, idempotency_key)
        return AdapterReceipt(adapter_id=detail.id, state=detail.state, version=detail.version)


@router.post("/{adapter_id}/retire", response_model=AdapterDetail)
async def retire_detail(
    request: Request,
    adapter_id: uuid.UUID,
    body: AdapterRetireRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> AdapterDetail:
    enabled(request)
    async with session_scope() as session:
        return await curate_adapter(session, principal, adapter_id, body, idempotency_key)
