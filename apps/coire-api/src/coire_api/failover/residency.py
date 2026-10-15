"""Join authoritative instances/artifacts with authenticated native process observations."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    InstanceMemberRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeRow,
    ShardGroupRow,
    VariantCopyRow,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.failover_residency import sharded_placement, single_placement
from coire_core.models.engine import EngineStatus
from coire_core.models.failover import FailoverPlacement
from coire_core.models.instance import InstanceState
from coire_core.models.sharding import ShardGroupStatus
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


async def resident_placements(
    session: AsyncSession,
    settings: Settings,
    model_ids: list[UUID],
) -> dict[UUID, FailoverPlacement]:
    rows = (
        await session.execute(
            select(ModelInstanceRow, InstanceMemberRow, NodeRow, VariantCopyRow)
            .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
            .join(NodeRow, NodeRow.id == InstanceMemberRow.node_id)
            .join(ModelVariantRow, ModelVariantRow.id == ModelInstanceRow.variant_id)
            .join(
                VariantCopyRow,
                (VariantCopyRow.variant_id == ModelVariantRow.id)
                & (VariantCopyRow.node_id == NodeRow.id),
            )
            .where(
                ModelInstanceRow.model_id.in_(model_ids),
                ModelInstanceRow.state == InstanceState.READY,
                ModelInstanceRow.adapter_id.is_(None),
                ModelVariantRow.validated.is_(True),
                ModelVariantRow.published.is_(True),
                ModelVariantRow.backend == "mlx_lm",
                VariantCopyRow.verified.is_(True),
            )
            .order_by(ModelInstanceRow.id, InstanceMemberRow.rank)
        )
    ).all()
    placements: dict[UUID, FailoverPlacement] = {}
    async with NodeClient(settings, timeout=2) as client:
        engines: dict[str, list[EngineStatus]] = {}
        for instance, member, node, copy in rows:
            if instance.model_id in placements or node.name not in {"coire-edge-a", "coire-edge-b"}:
                continue
            placement: FailoverPlacement | None
            try:
                if instance.policy.startswith("single:") and member.engine_id is not None:
                    if node.name not in engines:
                        engines[node.name] = await client.list_engines(node.name)
                    engine = next(
                        (e for e in engines[node.name] if e.engine_id == member.engine_id), None
                    )
                    if engine is None:
                        continue
                    placement = single_placement(instance.id, node.name, engine)
                elif instance.policy.startswith("sharded:"):
                    copies = [
                        (n.name, c.manifest_sha256) for i, _, n, c in rows if i.id == instance.id
                    ]
                    if (
                        len(copies) != 2
                        or {name for name, _ in copies} != {"coire-edge-a", "coire-edge-b"}
                        or copy.manifest_sha256 is None
                        or {digest for _, digest in copies} != {copy.manifest_sha256}
                    ):
                        continue
                    group = await session.scalar(
                        select(ShardGroupRow).where(ShardGroupRow.instance_id == instance.id)
                    )
                    if group is None:
                        continue
                    names = ("coire-edge-a", "coire-edge-b")
                    statuses: list[ShardGroupStatus] = await asyncio.gather(
                        *(client.shard_group(name, group.id) for name in names)
                    )
                    placement = sharded_placement(dict(zip(names, statuses, strict=True)))
                    if statuses[0].command_id != group.command_id:
                        continue
                else:
                    continue
                if (
                    placement is not None
                    and placement.instance_id == instance.id
                    and placement.target.model_id == instance.model_id
                    and placement.target.variant_id == instance.variant_id
                    and placement.target.base_manifest_sha256 == copy.manifest_sha256
                ):
                    placements[instance.model_id] = placement
            except (NodeError, ValueError):
                logger.info(
                    "failover residency unavailable", extra={"instance_id": str(instance.id)}
                )
    return placements
