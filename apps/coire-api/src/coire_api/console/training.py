"""Content-free bounded training Jobs projection, including disabled-mode history."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    MemoryReservationRow,
    TrainingAttemptRow,
    TrainingJobRow,
    TrainingMetricRow,
)
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingValidationError
from coire_core.models.console import CursorPage, TrainingActivityItem
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training import TERMINAL_TRAINING_STATES, TrainingSpecV3, parse_training_spec
from coire_core.models.training_types import TrainingId


@observed("coire.api.training.activity")
async def project_training_activity(
    session: AsyncSession,
    principal: Principal,
    *,
    limit: int = 25,
    cursor: str | None = None,
) -> CursorPage[TrainingActivityItem]:
    await authorize_live_training_action(session, principal)
    if not 1 <= limit <= 100:
        raise TrainingValidationError("Activity limit is outside its bound")
    query = select(TrainingJobRow).where(TrainingJobRow.deleted_at.is_(None))
    if cursor is not None:
        try:
            timestamp, identity = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
            before = datetime.fromisoformat(timestamp)
            TypeAdapter(TrainingId).validate_python(identity)
            if before.tzinfo is None:
                raise ValueError
        except (binascii.Error, UnicodeError, ValueError, ValidationError):
            raise TrainingValidationError("Invalid training activity cursor") from None
        query = query.where(
            or_(
                TrainingJobRow.created_at < before,
                and_(TrainingJobRow.created_at == before, TrainingJobRow.id < identity),
            )
        )
    rows = list(
        await session.scalars(
            query.order_by(
                TrainingJobRow.created_at.desc(),
                TrainingJobRow.id.desc(),
            ).limit(limit + 1)
        )
    )
    selected = rows[:limit]
    identities = [row.id for row in selected]
    losses = (
        list(
            await session.scalars(
                select(TrainingMetricRow)
                .where(
                    TrainingMetricRow.job_id.in_(identities),
                    TrainingMetricRow.rolled_back.is_(False),
                )
                .distinct(TrainingMetricRow.job_id, TrainingMetricRow.kind)
                .order_by(
                    TrainingMetricRow.job_id,
                    TrainingMetricRow.kind,
                    TrainingMetricRow.recorded_at.desc(),
                    TrainingMetricRow.completed_update.desc(),
                )
            )
        )
        if selected
        else []
    )
    latest = {(row.job_id, row.kind): row.loss for row in losses}
    probes = {(row.job_id, row.kind): row.metric.get("probe") for row in losses}
    reservations: dict[str, int] = {}
    if selected:
        reservation_rows = await session.execute(
            select(TrainingAttemptRow.job_id, func.sum(MemoryReservationRow.bytes))
            .join(MemoryReservationRow, MemoryReservationRow.holder_id == TrainingAttemptRow.id)
            .where(
                TrainingAttemptRow.job_id.in_(identities),
                MemoryReservationRow.holder_type == ReservationHolder.TRAINING,
                MemoryReservationRow.state.in_(
                    [
                        MemoryReservationState.PENDING,
                        MemoryReservationState.HELD,
                        MemoryReservationState.RELEASING,
                    ]
                ),
            )
            .group_by(TrainingAttemptRow.job_id)
        )
        reservations = dict(reservation_rows.tuples().all())
    items = []
    for row in selected:
        from coire_api.evaluation.links import for_job

        spec = parse_training_spec(row.submitted_spec)
        items.append(
            TrainingActivityItem.model_validate(
                {
                    "objective": spec.objective if isinstance(spec, TrainingSpecV3) else None,
                    "preference_probe": probes.get((row.id, "train"))
                    if isinstance(spec, TrainingSpecV3)
                    else None,
                    "job_id": row.id,
                    "evaluation_groups": await for_job(session, row.id),
                    "owner_id": row.owner_user_id,
                    "model_id": row.model_id,
                    "variant_id": row.base_variant_id,
                    "state": row.state,
                    "completed_update": row.completed_update,
                    "total_updates": spec.optim.updates,
                    "reserved_bytes": int(reservations.get(row.id, 0)),
                    "can_stop": row.state not in TERMINAL_TRAINING_STATES
                    and row.state != "cancelling",
                    "safe_reason": row.safe_reason,
                    "version": row.version,
                    "adapter_slug": row.output_slug,
                    "started_at": row.created_at,
                    "latest_train_loss": latest.get((row.id, "train")),
                    "latest_validation_loss": latest.get((row.id, "validation")),
                }
            )
        )
    next_cursor = None
    if len(rows) > limit and selected:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(
            f"{last.created_at.isoformat()}|{last.id}".encode()
        ).decode()
    return CursorPage(items=items, next_cursor=next_cursor)
