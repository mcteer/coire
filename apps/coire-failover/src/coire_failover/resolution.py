"""Resolve inference strictly from the signed roster and a live resident-model observation."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from coire_core.models.failover import FailoverModel, FailoverSnapshot


class FailoverModelUnavailable(LookupError):
    """The requested registry model is not safe to serve in degraded mode."""


def resolve_resident_model(
    snapshot: FailoverSnapshot,
    model_id: UUID,
    *,
    resident_model_ids: frozenset[UUID],
) -> FailoverModel:
    """Return a published snapshot model only when live health proves it resident.

    Degraded authentication currently has no replicated per-user entitlement grants. Models
    carrying any entitlement requirement therefore fail closed until such claims are included
    in the signed snapshot contract.
    """
    model, destination = choose_relay(
        snapshot,
        model_id,
        local_resident_ids=resident_model_ids,
        peer_resident_ids=frozenset(),
    )
    if destination != "local":
        raise FailoverModelUnavailable(str(model_id))
    return model


def choose_relay(
    snapshot: FailoverSnapshot,
    model_id: UUID,
    *,
    local_resident_ids: frozenset[UUID],
    peer_resident_ids: frozenset[UUID] = frozenset(),
) -> tuple[FailoverModel, Literal["local", "peer"]]:
    """Pick the local node relay, or the peer relay when only the other Studio holds the model."""
    model = next((item for item in snapshot.models if item.id == model_id), None)
    if model is None or model.entitlement:
        raise FailoverModelUnavailable(str(model_id))
    if model.id in local_resident_ids:
        return model, "local"
    if model.id in peer_resident_ids:
        return model, "peer"
    raise FailoverModelUnavailable(str(model_id))
