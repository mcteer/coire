"""Content-free immutable ancestry, independent of live parent availability."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import AdapterLineageRow, TrainingAdapterRow, TrainingJobRow
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingConflict, TrainingNotFound
from coire_core.models.adapters import InferenceTarget
from coire_core.models.feedback import AdapterLineage, AdapterLineageEntry
from coire_core.models.training import ResolvedTrainingSpecV3, parse_resolved_training_spec


def compose_lineage(
    adapter_id: uuid.UUID,
    base: InferenceTarget,
    parent: InferenceTarget | None,
    parent_entry: AdapterLineageEntry | None,
    prior: AdapterLineage | None,
) -> AdapterLineage:
    ancestors = ([parent_entry] if parent_entry else []) + (prior.ancestors if prior else [])
    if base.adapter_id is not None or bool(parent) != bool(parent_entry):
        raise TrainingConflict("Ancestry requires an exact bare base and parent snapshot")
    if parent is not None and (
        parent.adapter_id is None
        or parent_entry is None
        or parent_entry.target != parent
        or parent_entry.adapter_id != parent.adapter_id
        or (parent.model_id, parent.variant_id, parent.base_manifest_sha256)
        != (base.model_id, base.variant_id, base.base_manifest_sha256)
    ):
        raise TrainingConflict("Ancestry parent differs from its frozen base")
    if prior is not None and (
        parent is None or prior.adapter_id != parent.adapter_id or prior.base != base
    ):
        raise TrainingConflict("Ancestry snapshot differs from exact parent")
    identities = [item.adapter_id for item in ancestors]
    if len(ancestors) > 32 or adapter_id in identities or len(set(identities)) != len(identities):
        raise TrainingConflict("Ancestry is cyclic or exceeds maximum depth")
    return AdapterLineage(adapter_id=adapter_id, base=base, parent=parent, ancestors=ancestors)


async def persist_lineage(
    session: AsyncSession, row: TrainingAdapterRow, resolved: ResolvedTrainingSpecV3
) -> None:
    base = resolved.initial_target.model_copy(
        update={"adapter_id": None, "adapter_manifest_sha256": None}
    )
    parent_target = (
        resolved.initial_target if resolved.initial_target.adapter_id is not None else None
    )
    parent_entry = None
    prior = None
    if parent_target is not None:
        parent = await session.get(
            TrainingAdapterRow,
            parent_target.adapter_id,
            with_for_update=True,
            populate_existing=True,
        )
        if parent is None or parent.manifest_sha256 != parent_target.adapter_manifest_sha256:
            raise TrainingConflict("Frozen initial adapter identity changed")
        origin = await session.get(TrainingJobRow, parent.source_job_id)
        if origin is None or origin.resolved_spec is None:
            raise TrainingConflict("Initial adapter lineage is unavailable")
        origin_spec = parse_resolved_training_spec(origin.resolved_spec)
        if payload_digest(origin_spec) != parent.resolved_spec_sha256:
            raise TrainingConflict("Initial adapter lineage identity changed")
        objective = parent.objective
        if objective not in {"sft", "dpo", "orpo"}:
            raise TrainingConflict("Initial adapter objective is invalid")
        parent_entry = AdapterLineageEntry(
            adapter_id=parent.id,
            target=parent_target,
            objective="dpo" if objective == "dpo" else "orpo" if objective == "orpo" else "sft",
            source_job_id=parent.source_job_id,
            dataset_ids=[item.dataset_id for item in origin_spec.datasets],
            resolved_spec_sha256=parent.resolved_spec_sha256,
            created_at=parent.created_at,
        )
        prior_row = await session.get(AdapterLineageRow, parent.id)
        if prior_row is not None:
            prior = AdapterLineage.model_validate(prior_row.lineage)
        elif parent.objective != "sft":
            raise TrainingConflict("Initial preference adapter ancestry is unavailable")
    document = compose_lineage(row.id, base, parent_target, parent_entry, prior)
    prior_row = await session.get(AdapterLineageRow, row.id)
    if prior_row is not None:
        if AdapterLineage.model_validate(prior_row.lineage) != document:
            raise TrainingConflict("Adapter ancestry is immutable")
        return
    await session.flush()
    session.add(
        AdapterLineageRow(
            adapter_id=row.id,
            parent_adapter_id=parent_target.adapter_id if parent_target else None,
            depth=len(document.ancestors),
            lineage=document.model_dump(mode="json"),
        )
    )
    await session.flush()


async def get_lineage(session: AsyncSession, identity: uuid.UUID) -> AdapterLineage:
    adapter = await session.get(TrainingAdapterRow, identity)
    if adapter is None or adapter.purpose == "evaluation":
        raise TrainingNotFound()
    row = await session.get(AdapterLineageRow, identity)
    if row is None:
        return AdapterLineage(
            adapter_id=identity,
            base=InferenceTarget(
                model_id=adapter.model_id,
                variant_id=adapter.base_variant_id,
                base_manifest_sha256=adapter.base_manifest_sha256,
            ),
            parent=None,
            ancestors=[],
        )
    return AdapterLineage.model_validate(row.lineage)
