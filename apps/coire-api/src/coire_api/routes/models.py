"""The user-facing model listing.

The one route in this feature that is not admin-only. It answers what the picker needs to
choose a model and nothing more: no repository id, no paths, no copies, no failure reasons.
Feature 003's `/v1/models` reads the same shape.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import select

from coire_api.auth import CurrentPrincipal
from coire_api.db import EngineProcessRow, ModelRow, NodeRow
from coire_api.deps import SessionDep
from coire_api.registry import service
from coire_core.models.registry import ModelListing

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["models"])


@router.get("/models", response_model=list[ModelListing])
async def list_models(principal: CurrentPrincipal, session: SessionDep) -> list[ModelListing]:
    """Models the caller may see, with live load state.

    An admin sees everything; anyone else sees only published, ready, unentitled models
    (spec US5 scenario 1). Existence is not disclosed: an unpublished model is simply absent.
    """
    rows = (await session.execute(select(ModelRow).order_by(ModelRow.display_name))).scalars().all()
    visible = [m for m in rows if service.visible_to(is_admin=principal.is_admin, model=m)]
    if not visible:
        return []

    node_names = {n.id: n.name for n in (await session.execute(select(NodeRow))).scalars().all()}
    engines = (
        (
            await session.execute(
                select(EngineProcessRow).where(
                    EngineProcessRow.model_id.in_([m.id for m in visible])
                )
            )
        )
        .scalars()
        .all()
    )
    by_model: dict[object, list[EngineProcessRow]] = {}
    for engine in engines:
        by_model.setdefault(engine.model_id, []).append(engine)

    return [service.to_listing(m, by_model.get(m.id, []), node_names=node_names) for m in visible]
