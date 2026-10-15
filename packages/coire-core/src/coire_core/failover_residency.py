"""Exact immutable process/group identities and challenge-bound live rank evidence."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from uuid import UUID

from coire_core.failover_crypto import verify_ed25519
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState, EngineStatus
from coire_core.models.failover import (
    FailoverModel,
    FailoverRankIdentity,
    FailoverRankObservation,
    FailoverRankProbe,
    FailoverResidentEngine,
    FailoverShardedPlacement,
    FailoverSinglePlacement,
)
from coire_core.models.sharding import ShardGroupState, ShardGroupStatus


def sharded_placement(statuses: dict[str, ShardGroupStatus]) -> FailoverShardedPlacement | None:
    if set(statuses) != {"coire-edge-a", "coire-edge-b"}:
        return None
    first = statuses["coire-edge-a"]
    if first.target is None or first.target.adapter_id is not None or first.command_id is None:
        return None
    ranks = []
    for node, status in sorted(statuses.items()):
        rank = next((r for r in status.ranks if r.node_name == node), None)
        if (
            status.state is not ShardGroupState.READY
            or status.group_id != first.group_id
            or status.instance_id != first.instance_id
            or status.target != first.target
            or status.command_id != first.command_id
            or status.mode != first.mode
            or rank is None
            or rank.pid is None
            or rank.process_create_time is None
        ):
            return None
        ranks.append(
            FailoverRankIdentity.model_validate(
                {
                    "rank": rank.rank,
                    "node": node,
                    "generation": process_generation(
                        instance_id=status.instance_id,
                        engine_id=status.group_id,
                        group_id=status.group_id,
                        node=node,
                        pid=rank.pid,
                        process_create_time=rank.process_create_time,
                        target=first.target,
                        rank=rank.rank,
                    ),
                }
            )
        )
    encoded = json.dumps([r.model_dump(mode="json") for r in ranks], sort_keys=True).encode()
    return FailoverShardedPlacement(
        mode="tp" if first.mode.value == "tp" else "pp",
        instance_id=first.instance_id,
        engine_id=first.group_id,
        group_id=first.group_id,
        generation=hashlib.sha256(encoded).hexdigest(),
        target=first.target,
        ranks=ranks,
    )


def eligible_model(model: FailoverModel) -> bool:
    return (
        not model.entitlement
        and model.capabilities == {"text_chat"}
        and model.placement is not None
    )


def resident_matches(model: FailoverModel, resident: FailoverResidentEngine) -> bool:
    return (
        eligible_model(model)
        and resident.placement == model.placement
        and model.placement is not None
        and resident.engine_id == model.placement.engine_id
        and resident.slug == model.slug
    )


def single_placement(
    instance_id: UUID,
    node: str,
    engine: EngineStatus,
) -> FailoverSinglePlacement | None:
    if (
        engine.state is not EngineState.READY
        or engine.target is None
        or engine.target.adapter_id is not None
        or engine.pid is None
        or engine.process_create_time is None
        or engine.engine_id is None
        or node not in {"coire-edge-a", "coire-edge-b"}
    ):
        return None
    return FailoverSinglePlacement.model_validate(
        {
            "mode": "single",
            "instance_id": instance_id,
            "engine_id": engine.engine_id,
            "node": node,
            "target": engine.target,
            "generation": process_generation(
                instance_id=instance_id,
                engine_id=engine.engine_id,
                node=node,
                pid=engine.pid,
                process_create_time=engine.process_create_time,
                target=engine.target,
            ),
        }
    )


def process_generation(
    *,
    instance_id: UUID,
    engine_id: UUID,
    node: str,
    pid: int,
    process_create_time: float,
    target: InferenceTarget,
    group_id: UUID | None = None,
    rank: int | None = None,
) -> str:
    """Hash trusted local process facts; never accept a caller-authored generation."""
    if (
        node not in {"coire-edge-a", "coire-edge-b"}
        or pid <= 0
        or not math.isfinite(process_create_time)
        or process_create_time <= 0
    ):
        raise ValueError("generation requires a Studio's positive, finite local process identity")
    if (group_id is None) != (rank is None) or (rank is not None and rank not in {0, 1}):
        raise ValueError("group and rank identity must be supplied together")
    fields = {
        "instance_id": str(instance_id),
        "engine_id": str(engine_id),
        "node": node,
        "pid": pid,
        "process_create_time": process_create_time,
        "target": target.model_dump(mode="json"),
        "group_id": str(group_id) if group_id else None,
        "rank": rank,
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def rank_observation_current(
    probe: FailoverRankProbe,
    observation: FailoverRankObservation,
    public_key: str,
    *,
    now: datetime | None = None,
) -> bool:
    moment = now or datetime.now(UTC)
    expected = next((rank for rank in probe.placement.ranks if rank.node == observation.host), None)
    return (
        expected is not None
        and probe.issued_at <= moment < probe.expires_at
        and observation.issued_at >= probe.issued_at
        and observation.issued_at <= moment < observation.expires_at <= probe.expires_at
        and observation.challenge == probe.challenge
        and observation.epoch == probe.epoch
        and observation.instance_id == probe.placement.instance_id
        and observation.group_id == probe.placement.group_id
        and observation.group_generation == probe.placement.generation
        and observation.target == probe.placement.target
        and observation.rank == expected
        and observation.healthy
        and observation.link_healthy
        and verify_ed25519(observation.canonical_bytes(), observation.signature, public_key)
    )
