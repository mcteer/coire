"""Registry-only model resolution; caller strings stop at this boundary."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import EngineProcessRow, ModelCopyRow, ModelRow, NodeRow
from coire_api.gateway.telemetry import tracer
from coire_core.models.engine import EngineState
from coire_core.models.registry import ModelState, Visibility


class ModelNotFoundError(Exception):
    """Externally uniform refusal for missing or invisible models."""


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    model_id: uuid.UUID
    slug: str
    context_window: int | None
    model_path: str | None
    engine_id: uuid.UUID | None
    node: str | None
    engine_url: str | None


def _visible(model: ModelRow, principal: Principal) -> bool:
    if principal.is_admin:
        return model.state is not ModelState.RETIRED
    if model.state is not ModelState.READY or model.visibility is not Visibility.PUBLISHED:
        return False
    return not model.entitlement or set(model.entitlement).issubset(principal.scopes)


async def resolve_model(
    session: AsyncSession, requested_id: uuid.UUID, principal: Principal
) -> ResolvedModel:
    with tracer.start_as_current_span("coire.gateway.resolve") as span:
        span.set_attribute("coire.model.requested_id", str(requested_id))
        model = await session.get(ModelRow, requested_id)
        if model is None or not _visible(model, principal):
            span.set_attribute("coire.gateway.resolution", "refused")
            raise ModelNotFoundError
        span.set_attribute("coire.model.id", str(model.id))

    result = await session.execute(
        select(EngineProcessRow, NodeRow, ModelCopyRow)
        .join(NodeRow, NodeRow.id == EngineProcessRow.node_id)
        .join(
            ModelCopyRow,
            (ModelCopyRow.model_id == model.id) & (ModelCopyRow.node_id == NodeRow.id),
        )
        .where(
            EngineProcessRow.model_id == model.id,
            EngineProcessRow.state.in_([EngineState.READY, EngineState.STARTING]),
            ModelCopyRow.verified.is_(True),
        )
        .order_by(EngineProcessRow.started_at)
        .limit(1)
    )
    target = result.one_or_none()
    if target is None:
        return ResolvedModel(model.id, model.slug, model.context_window, None, None, None, None)
    engine, node, copy = target
    return ResolvedModel(
        model.id,
        model.slug,
        model.context_window,
        copy.path,
        engine.id,
        node.name,
        f"http://{node.name}.lab:9400/node/engines/{engine.id}/proxy",
    )


async def retry_after_seconds(session: AsyncSession, model_id: uuid.UUID, *, fallback: int) -> int:
    """Return the most recent measured warm-up, falling back only before one exists."""
    measured = await session.scalar(
        select(EngineProcessRow.load_seconds)
        .where(
            EngineProcessRow.model_id == model_id,
            EngineProcessRow.load_seconds.is_not(None),
        )
        .order_by(EngineProcessRow.started_at.desc())
        .limit(1)
    )
    return max(1, math.ceil(float(measured))) if measured is not None else fallback
