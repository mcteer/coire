"""Inert preference preflight identities and exact registered initialization artifacts."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    AdapterLineageRow,
    NodeRow,
    TrainingAdapterRow,
    TrainingArtifactCopyRow,
    TrainingJobRow,
    TrainingMeasurementRow,
)
from coire_core.errors import TrainingConflict, TrainingNotFound, TrainingValidationError
from coire_core.models.adapters import InferenceTarget
from coire_core.models.datasets import SplitManifest
from coire_core.models.preference import PreferenceSplitManifest
from coire_core.models.training import (
    TERMINAL_TRAINING_STATES,
    TrainingSpecDocument,
    TrainingSpecV3,
    parse_resolved_training_spec,
)
from coire_core.preference_data import preference_split_digest
from coire_core.settings import Settings
from coire_core.training_data import split_digest


async def require_preference_capabilities(settings: Settings) -> None:
    from coire_api.nodes_client import NodeClient
    from coire_core.errors import TrainingUnavailable

    async with NodeClient(settings) as client:
        try:
            for node in ("coire-edge-a", "coire-edge-b"):
                status = await client.health(node)
                capabilities = getattr(status, "training_capabilities", None)
                if capabilities is None or 3 not in capabilities.spec_versions:
                    raise TrainingUnavailable("Both Studios must support preference training v3")
        except TrainingUnavailable:
            raise
        except Exception:
            raise TrainingUnavailable(
                "Preference training capability negotiation is unavailable"
            ) from None


async def resolve_initial_target(
    session: AsyncSession, spec: TrainingSpecV3, base_manifest_sha256: str
) -> InferenceTarget:
    if spec.init_adapter is None:
        return InferenceTarget(
            model_id=spec.model.model_id,
            variant_id=spec.model.variant_id,
            base_manifest_sha256=base_manifest_sha256,
        )
    parent = await session.get(
        TrainingAdapterRow, spec.init_adapter, populate_existing=True, with_for_update=True
    )
    if parent is None:
        raise TrainingNotFound()
    if (
        parent.state != "ready"
        or parent.purpose != "serving"
        or parent.model_id != spec.model.model_id
        or parent.base_variant_id != spec.model.variant_id
        or parent.base_manifest_sha256 != base_manifest_sha256
        or parent.manifest_sha256 is None
        or parent.parameterization != spec.parameterization.kind
    ):
        raise TrainingConflict("Initial adapter is unavailable or differs from the exact base")
    origin = await session.get(TrainingJobRow, parent.source_job_id, populate_existing=True)
    if origin is None or origin.resolved_spec is None:
        raise TrainingConflict("Initial adapter has no immutable parameterization evidence")
    try:
        resolved = parse_resolved_training_spec(origin.resolved_spec)
    except ValueError:
        raise TrainingConflict("Initial adapter parameterization evidence is invalid") from None
    if (
        resolved.spec.parameterization != spec.parameterization
        or resolved.base_manifest_sha256 != base_manifest_sha256
        or resolved.spec.model != spec.model
        or parent.resolved_spec_sha256 != origin.resolved_sha256
    ):
        raise TrainingValidationError("Initial adapter trainable configuration differs from intent")
    lineage = await session.get(AdapterLineageRow, parent.id, populate_existing=True)
    if parent.objective != "sft" and lineage is None:
        raise TrainingConflict("Initial preference adapter has no immutable ancestry")
    if lineage is not None and lineage.depth >= 32:
        raise TrainingValidationError("Initial adapter exceeds the maximum ancestry depth")
    copies = list(
        (
            await session.execute(
                select(NodeRow.name, TrainingArtifactCopyRow.manifest_sha256)
                .join(TrainingArtifactCopyRow, TrainingArtifactCopyRow.node_id == NodeRow.id)
                .where(
                    TrainingArtifactCopyRow.adapter_id == parent.id,
                    TrainingArtifactCopyRow.artifact_id == parent.id,
                    TrainingArtifactCopyRow.state == "verified",
                    TrainingArtifactCopyRow.verified_at.is_not(None),
                )
            )
        ).all()
    )
    if {name: digest for name, digest in copies if name in {"coire-edge-a", "coire-edge-b"}} != {
        "coire-edge-a": parent.manifest_sha256,
        "coire-edge-b": parent.manifest_sha256,
    }:
        raise TrainingConflict("Initial adapter requires exact verified copies on both Studios")
    return InferenceTarget(
        model_id=parent.model_id,
        variant_id=parent.base_variant_id,
        adapter_id=parent.id,
        base_manifest_sha256=base_manifest_sha256,
        adapter_manifest_sha256=parent.manifest_sha256,
    )


def objective_split(
    spec: TrainingSpecDocument, value: object
) -> SplitManifest | PreferenceSplitManifest:
    if isinstance(spec, TrainingSpecV3):
        return PreferenceSplitManifest.model_validate(value)
    return SplitManifest.model_validate(value)


def objective_split_digest(split: SplitManifest | PreferenceSplitManifest) -> str:
    return (
        preference_split_digest(split)
        if isinstance(split, PreferenceSplitManifest)
        else split_digest(split)
    )


def require_dataset_objective(spec: TrainingSpecDocument, format: str) -> None:
    if (format == "preference") != isinstance(spec, TrainingSpecV3):
        raise TrainingValidationError("Dataset format differs from the recipe objective")


async def initial_adapter_pinned(session: AsyncSession, adapter_id: uuid.UUID) -> bool:
    """Frozen live job/measurement metadata is the durable initialization pin.

    Submission and retirement hold the parent row lock, so publication of this
    reference cannot race the corresponding availability mutation.
    """
    job = await session.scalar(
        select(TrainingJobRow.id)
        .where(
            TrainingJobRow.deleted_at.is_(None),
            TrainingJobRow.state.not_in([str(state) for state in TERMINAL_TRAINING_STATES]),
            TrainingJobRow.resolved_spec["spec"]["schema_version"].as_integer() == 3,
            TrainingJobRow.resolved_spec["initial_target"]["adapter_id"].as_string()
            == str(adapter_id),
        )
        .limit(1)
    )
    if job is not None:
        return True
    measurement = await session.scalar(
        select(TrainingMeasurementRow.id)
        .where(
            TrainingMeasurementRow.state.in_(["queued", "running"]),
            TrainingMeasurementRow.request["spec"]["schema_version"].as_integer() == 3,
            TrainingMeasurementRow.request["spec"]["init_adapter"].as_string() == str(adapter_id),
        )
        .limit(1)
    )
    return measurement is not None
