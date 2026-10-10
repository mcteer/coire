"""Fresh batched registry inventory for private, admin-owned measurements."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, bindparam, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only

from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAdapterRow,
    VariantCopyRow,
)
from coire_api.gateway.resolution import ResolvedModel
from coire_api.registry.service import is_chat_backend
from coire_core.errors import TrainingConflict
from coire_core.models.acquisition import VariantState
from coire_core.models.engine import EngineRenderingIdentity, EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import EngineBackend, ModelState, VisualCapability
from coire_core.models.training import TrainingResidentTarget


@dataclass(frozen=True)
class MeasurementResident:
    resolved: ResolvedModel
    reservation: MemoryReservationRow
    instance: ModelInstanceRow


def _resident_inventory_statement() -> Select[
    tuple[
        MemoryReservationRow,
        InstanceMemberRow,
        ModelInstanceRow,
        ModelRow,
        ModelVariantRow,
        EngineProcessRow,
        VariantCopyRow,
        TrainingAdapterRow,
        NodeRow,
        int,
        list[str],
    ]
]:
    member_count = (
        select(func.count())
        .select_from(InstanceMemberRow)
        .where(InstanceMemberRow.instance_id == ModelInstanceRow.id)
        .correlate(ModelInstanceRow)
        .scalar_subquery()
    )
    verified_copy = aliased(VariantCopyRow)
    verified_digests = (
        select(func.array_agg(func.distinct(verified_copy.manifest_sha256)))
        .where(
            verified_copy.variant_id == ModelVariantRow.id,
            verified_copy.verified.is_(True),
            verified_copy.manifest_sha256.is_not(None),
        )
        .correlate(ModelVariantRow)
        .scalar_subquery()
    )
    return (
        select(
            MemoryReservationRow,
            InstanceMemberRow,
            ModelInstanceRow,
            ModelRow,
            ModelVariantRow,
            EngineProcessRow,
            VariantCopyRow,
            TrainingAdapterRow,
            NodeRow,
            member_count,
            verified_digests,
        )
        .join(NodeRow, NodeRow.id == MemoryReservationRow.node_id)
        .outerjoin(
            InstanceMemberRow,
            InstanceMemberRow.reservation_id == MemoryReservationRow.id,
        )
        .outerjoin(ModelInstanceRow, ModelInstanceRow.id == InstanceMemberRow.instance_id)
        .outerjoin(ModelRow, ModelRow.id == ModelInstanceRow.model_id)
        .outerjoin(ModelVariantRow, ModelVariantRow.id == ModelInstanceRow.variant_id)
        .outerjoin(EngineProcessRow, EngineProcessRow.id == InstanceMemberRow.engine_id)
        .outerjoin(
            VariantCopyRow,
            (VariantCopyRow.variant_id == ModelInstanceRow.variant_id)
            & (VariantCopyRow.node_id == MemoryReservationRow.node_id),
        )
        .outerjoin(TrainingAdapterRow, TrainingAdapterRow.id == ModelInstanceRow.adapter_id)
        .where(
            MemoryReservationRow.node_id.in_(bindparam("measurement_node_ids", expanding=True)),
            MemoryReservationRow.holder_type.in_(
                [ReservationHolder.MODEL, ReservationHolder.TRAINING]
            ),
            MemoryReservationRow.state.in_(
                [
                    MemoryReservationState.PENDING,
                    MemoryReservationState.HELD,
                    MemoryReservationState.RELEASING,
                ]
            ),
        )
        .options(
            load_only(
                InstanceMemberRow.id,
                InstanceMemberRow.instance_id,
                InstanceMemberRow.node_id,
                InstanceMemberRow.reservation_id,
                InstanceMemberRow.engine_id,
                InstanceMemberRow.port,
                raiseload=True,
            ),
            load_only(
                ModelRow.id,
                ModelRow.state,
                ModelRow.kind,
                ModelRow.backend,
                ModelRow.source,
                ModelRow.slug,
                ModelRow.context_window,
                ModelRow.visual_capability,
                raiseload=True,
            ),
            load_only(
                ModelVariantRow.id,
                ModelVariantRow.model_id,
                ModelVariantRow.state,
                ModelVariantRow.validated,
                raiseload=True,
            ),
            load_only(
                EngineProcessRow.id,
                EngineProcessRow.state,
                EngineProcessRow.instance_id,
                EngineProcessRow.node_id,
                EngineProcessRow.port,
                EngineProcessRow.model_id,
                EngineProcessRow.variant_id,
                EngineProcessRow.adapter_id,
                EngineProcessRow.rendering_identity,
                raiseload=True,
            ),
            load_only(
                VariantCopyRow.id,
                VariantCopyRow.verified,
                VariantCopyRow.manifest_sha256,
                VariantCopyRow.path,
                raiseload=True,
            ),
            load_only(
                TrainingAdapterRow.id,
                TrainingAdapterRow.state,
                TrainingAdapterRow.purpose,
                TrainingAdapterRow.selector,
                TrainingAdapterRow.slug,
                TrainingAdapterRow.model_id,
                TrainingAdapterRow.base_variant_id,
                TrainingAdapterRow.base_manifest_sha256,
                TrainingAdapterRow.manifest_sha256,
                raiseload=True,
            ),
            load_only(NodeRow.id, NodeRow.name, raiseload=True),
        )
        .with_for_update(of=MemoryReservationRow)
        .execution_options(populate_existing=True)
    )


# Only the SQL structure is reused. Every execution reads current database rows.
_RESIDENT_INVENTORY = _resident_inventory_statement()


async def resolve_measurement_residents(
    session: AsyncSession,
    node_ids: Sequence[uuid.UUID],
    targets: Sequence[TrainingResidentTarget],
    engine_ids: dict[uuid.UUID, uuid.UUID],
    training_hold_ids: set[uuid.UUID],
    measurement_id: uuid.UUID,
    node_bindings: dict[uuid.UUID, str],
) -> dict[uuid.UUID, MeasurementResident]:
    """Check every counted model hold, including missing/extra/legacy occupancy.

    The caller holds current administrator and node admission locks. No cached
    authority survives the transaction. Outer joins retain malformed occupancy
    so missing registry or engine rows refuse instead of disappearing.
    """
    rows = (
        await session.execute(_RESIDENT_INVENTORY, {"measurement_node_ids": list(node_ids)})
    ).all()
    # Bound node identifiers are immutable workload identity, not live authority.
    # Current joined rows must still have every exact identifier/name pair.
    current_nodes = {row[8].id: row[8].name for row in rows}
    if current_nodes != node_bindings:
        raise TrainingConflict("Measurement node inventory changed")
    training_holds = [row[0] for row in rows if row[0].holder_type is ReservationHolder.TRAINING]
    if {hold.id for hold in training_holds} != training_hold_ids or any(
        hold.state is not MemoryReservationState.HELD
        or hold.holder_id != f"measurement:{measurement_id}"
        for hold in training_holds
    ):
        raise TrainingConflict("Dedicated measurement admission hold ended")
    rows = [row for row in rows if row[0].holder_type is ReservationHolder.MODEL]
    expected = {target.instance_id: target.target for target in targets}
    if len(rows) != len(expected):
        raise TrainingConflict("Measurement resident set changed")
    resolved: dict[uuid.UUID, MeasurementResident] = {}
    for hold, member, instance, model, variant, engine, copy, adapter, node, count, digests in rows:
        if (
            member is None
            or instance is None
            or model is None
            or variant is None
            or instance.id not in expected
            or count != 1
            or instance.id in resolved
            or instance.state is not InstanceState.READY
            or hold.state is not MemoryReservationState.HELD
            or member.node_id != hold.node_id
            or hold.holder_id != str(instance.id)
        ):
            raise TrainingConflict("Measurement resident set changed")
        target = expected[instance.id]
        if (
            (instance.model_id, instance.variant_id, instance.adapter_id)
            != (target.model_id, target.variant_id, target.adapter_id)
            or model.state is not ModelState.READY
            or not is_chat_backend(model)
            or (model.source or "studio") != "studio"
            or variant.model_id != model.id
            or variant.state is not VariantState.READY
            or not variant.validated
        ):
            raise TrainingConflict("Exact single-node resident target changed")
        if (
            engine is None
            or engine.state is not EngineState.READY
            or engine.instance_id != instance.id
            or engine.node_id != member.node_id
            or engine.port != member.port
            or (engine.model_id, engine.variant_id, engine.adapter_id)
            != (target.model_id, target.variant_id, target.adapter_id)
            or engine_ids.get(instance.id) != engine.id
        ):
            raise TrainingConflict("Exact single-node resident engine changed")
        if (
            copy is None
            or not copy.verified
            or copy.manifest_sha256 != target.base_manifest_sha256
            or set(digests or []) != {target.base_manifest_sha256}
        ):
            raise TrainingConflict("Resident base artifact changed")
        if target.adapter_id is not None and (
            adapter is None
            or adapter.state != "ready"
            or adapter.purpose == "evaluation"
            or adapter.selector != f"{model.id}@{adapter.slug}"
            or model.backend != "mlx_lm"
            or adapter.model_id != model.id
            or adapter.base_variant_id != variant.id
            or adapter.base_manifest_sha256 != target.base_manifest_sha256
            or adapter.manifest_sha256 != target.adapter_manifest_sha256
        ):
            raise TrainingConflict("Resident adapter artifact changed")
        result = ResolvedModel(
            model_id=model.id,
            slug=model.slug,
            context_window=model.context_window,
            model_path=copy.path,
            engine_id=engine.id,
            node=node.name,
            engine_url=f"http://{node.name}.lab:9400/node/engines/{engine.id}/proxy",
            backend=EngineBackend(model.backend),
            visual_capability=VisualCapability.model_validate(model.visual_capability)
            if model.visual_capability is not None
            else None,
            target=target,
            instance_id=instance.id,
            rendering_identity=EngineRenderingIdentity.model_validate(engine.rendering_identity)
            if engine.rendering_identity
            else None,
        )
        resolved[instance.id] = MeasurementResident(result, hold, instance)
    if set(resolved) != set(expected):
        raise TrainingConflict("Measurement resident set changed")
    return resolved
