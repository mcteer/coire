"""Fenced metadata commitments; artifact bytes and verification stay on Studios."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    NodeRow,
    TrainingArtifactCopyRow,
    TrainingCheckpointRow,
    TrainingJobRow,
)
from coire_api.training.events import append_event, current_attempt, current_job
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict
from coire_core.models.training import CheckpointDetail, TrainingCheckpointEvent
from coire_core.models.training_node import (
    TrainingArtifactManifest,
    TrainingArtifactVerificationReceipt,
)


@observed("coire.api.training.checkpoint.stage")
async def stage_checkpoint(
    session: AsyncSession, manifest: TrainingArtifactManifest
) -> TrainingCheckpointRow:
    if (
        manifest.kind != "checkpoint"
        or manifest.job_id is None
        or manifest.attempt_id is None
        or manifest.fence is None
    ):
        raise TrainingConflict("Only full-state checkpoints may be staged")
    job = await current_job(session, manifest.job_id, lock=True)
    attempt = await current_attempt(session, job, manifest.attempt_id, manifest.fence)
    if (
        manifest.runtime_sha256 != attempt.runtime_sha256
        or manifest.resolved_spec_sha256 != job.resolved_sha256
        or manifest.world_size != attempt.world_size
    ):
        raise TrainingConflict("Checkpoint immutable execution identity differs")
    existing = await session.get(
        TrainingCheckpointRow, manifest.artifact_id, populate_existing=True
    )
    if existing is not None:
        if (
            TrainingArtifactManifest.model_validate(existing.manifest).canonical_sha256()
            != manifest.canonical_sha256()
        ):
            raise TrainingConflict("Checkpoint identity is immutable")
        return existing
    duplicate = await session.scalar(
        select(TrainingCheckpointRow.id).where(
            TrainingCheckpointRow.job_id == job.id,
            TrainingCheckpointRow.attempt_id == attempt.id,
            TrainingCheckpointRow.completed_update == manifest.update,
        )
    )
    if duplicate is not None:
        raise TrainingConflict("Completed update already has a checkpoint identity")
    row = TrainingCheckpointRow(
        id=manifest.artifact_id,
        job_id=job.id,
        attempt_id=attempt.id,
        fence=attempt.fence,
        completed_update=manifest.update,
        manifest_sha256=manifest.canonical_sha256(),
        manifest=manifest.model_dump(mode="json"),
        total_bytes=manifest.total_bytes,
        state="staging",
    )
    session.add(row)
    await session.flush()
    return row


async def record_verified_copy(
    session: AsyncSession,
    manifest: TrainingArtifactManifest,
    receipt: TrainingArtifactVerificationReceipt,
    *,
    checkpoint_id: uuid.UUID | None = None,
    adapter_id: uuid.UUID | None = None,
) -> TrainingArtifactCopyRow:
    """Accept only an authenticated node executor's independently obtained verification."""
    if (
        receipt.artifact_id != manifest.artifact_id
        or receipt.manifest_sha256 != manifest.canonical_sha256()
        or receipt.verified_bytes != manifest.total_bytes
        or (checkpoint_id is None) == (adapter_id is None)
    ):
        raise TrainingConflict("Verification receipt differs from the complete artifact")
    if checkpoint_id is not None:
        candidate = await session.get(TrainingCheckpointRow, checkpoint_id)
        if candidate is None:
            raise TrainingConflict("Checkpoint has not been staged")
        await current_job(session, candidate.job_id, lock=True)
        checkpoint = await session.get(
            TrainingCheckpointRow, checkpoint_id, populate_existing=True, with_for_update=True
        )
        if (
            checkpoint is None
            or TrainingArtifactManifest.model_validate(checkpoint.manifest).canonical_sha256()
            != manifest.canonical_sha256()
            or checkpoint.state in {"purged", "corrupt"}
        ):
            raise TrainingConflict("Checkpoint verification identity changed")
    else:
        from coire_api.db import TrainingAdapterRow

        candidate_adapter = await session.get(TrainingAdapterRow, adapter_id)
        if candidate_adapter is None:
            raise TrainingConflict("Adapter has not been staged")
        await current_job(session, candidate_adapter.source_job_id, lock=True)
        adapter = await session.get(
            TrainingAdapterRow, adapter_id, populate_existing=True, with_for_update=True
        )
        if (
            adapter is None
            or adapter.manifest_sha256 != manifest.canonical_sha256()
            or adapter.state == "retired"
        ):
            raise TrainingConflict("Adapter verification identity changed")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == receipt.node))
    if node is None:
        raise TrainingConflict("Artifact copy is not on a declared Studio")
    existing = await session.scalar(
        select(TrainingArtifactCopyRow)
        .where(
            TrainingArtifactCopyRow.artifact_id == manifest.artifact_id,
            TrainingArtifactCopyRow.node_id == node.id,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.state in {"deleting", "purged"}:
            raise TrainingConflict("Artifact copy has an immutable cleanup intent")
        if (
            existing.manifest_sha256 != receipt.manifest_sha256
            or existing.total_bytes != receipt.verified_bytes
            or existing.checkpoint_id != checkpoint_id
            or existing.adapter_id != adapter_id
        ):
            raise TrainingConflict("Artifact copy identity is immutable")
        existing.state, existing.verified_at = "verified", datetime.now(UTC)
        return existing
    row = TrainingArtifactCopyRow(
        artifact_id=manifest.artifact_id,
        checkpoint_id=checkpoint_id,
        adapter_id=adapter_id,
        node_id=node.id,
        manifest_sha256=receipt.manifest_sha256,
        storage_key=str(manifest.artifact_id),
        total_bytes=receipt.verified_bytes,
        state="verified",
        verified_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def verified_nodes(
    session: AsyncSession, artifact_id: uuid.UUID, digest: str, total_bytes: int
) -> set[str]:
    rows = await session.execute(
        select(NodeRow.name)
        .join(TrainingArtifactCopyRow, TrainingArtifactCopyRow.node_id == NodeRow.id)
        .where(
            TrainingArtifactCopyRow.artifact_id == artifact_id,
            TrainingArtifactCopyRow.manifest_sha256 == digest,
            TrainingArtifactCopyRow.total_bytes == total_bytes,
            TrainingArtifactCopyRow.state == "verified",
            TrainingArtifactCopyRow.verified_at.is_not(None),
        )
    )
    return set(rows.scalars())


@observed("coire.api.training.checkpoint.commit")
async def commit_checkpoint(
    session: AsyncSession, checkpoint_id: uuid.UUID
) -> TrainingCheckpointRow:
    candidate = await session.get(TrainingCheckpointRow, checkpoint_id)
    if candidate is None:
        raise TrainingConflict("Checkpoint was not staged")
    job = await current_job(session, candidate.job_id, lock=True)
    checkpoint = await session.get(
        TrainingCheckpointRow, checkpoint_id, populate_existing=True, with_for_update=True
    )
    assert checkpoint is not None
    if checkpoint.state == "committed":
        from coire_api.evaluation.training import ensure_checkpoint_trigger
        from coire_core.settings import get_settings

        await ensure_checkpoint_trigger(session, job, checkpoint, settings=get_settings())
        return checkpoint
    await current_attempt(session, job, checkpoint.attempt_id, checkpoint.fence)
    manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
    if (
        checkpoint.state not in {"staging", "replicating"}
        or manifest.canonical_sha256() != checkpoint.manifest_sha256
    ):
        raise TrainingConflict("Checkpoint cannot be committed")
    if await verified_nodes(
        session, checkpoint.id, checkpoint.manifest_sha256, checkpoint.total_bytes
    ) != {"coire-edge-a", "coire-edge-b"}:
        raise TrainingConflict("Checkpoint awaits both independently verified complete copies")
    checkpoint.state, checkpoint.committed_at = "committed", datetime.now(UTC)
    latest = (
        await session.get(TrainingCheckpointRow, job.latest_checkpoint_id)
        if job.latest_checkpoint_id
        else None
    )
    if latest is None or latest.completed_update < checkpoint.completed_update:
        job.latest_checkpoint_id = checkpoint.id
    await append_event(
        session,
        job.id,
        TrainingCheckpointEvent(
            checkpoint_id=checkpoint.id,
            update=checkpoint.completed_update,
            manifest_sha256=checkpoint.manifest_sha256,
        ),
        attempt_id=checkpoint.attempt_id,
        fence=checkpoint.fence,
    )
    from coire_api.evaluation.training import ensure_checkpoint_trigger
    from coire_core.settings import get_settings

    await ensure_checkpoint_trigger(session, job, checkpoint, settings=get_settings())
    return checkpoint


async def recovery_checkpoint(
    session: AsyncSession, job: TrainingJobRow
) -> TrainingCheckpointRow | None:
    """Choose the newest full common bundle, skipping corrupt/incomplete candidates."""
    candidates = await session.scalars(
        select(TrainingCheckpointRow)
        .where(TrainingCheckpointRow.job_id == job.id, TrainingCheckpointRow.state == "committed")
        .order_by(
            TrainingCheckpointRow.completed_update.desc(), TrainingCheckpointRow.created_at.desc()
        )
        .with_for_update()
    )
    for row in candidates:
        try:
            manifest = TrainingArtifactManifest.model_validate(row.manifest)
        except ValueError:
            row.state = "corrupt"
            continue
        if (
            manifest.kind != "checkpoint"
            or manifest.canonical_sha256() != row.manifest_sha256
            or manifest.resolved_spec_sha256 != job.resolved_sha256
            or manifest.job_id != job.id
            or manifest.attempt_id != row.attempt_id
            or manifest.update != row.completed_update
            or manifest.fence != row.fence
            or await verified_nodes(session, row.id, row.manifest_sha256, row.total_bytes)
            != {"coire-edge-a", "coire-edge-b"}
        ):
            row.state = "corrupt"
            continue
        return row
    return None


async def checkpoint_detail(session: AsyncSession, row: TrainingCheckpointRow) -> CheckpointDetail:
    return CheckpointDetail.model_validate(
        {
            "id": row.id,
            "job_id": row.job_id,
            "attempt_id": row.attempt_id,
            "fence": row.fence,
            "update": row.completed_update,
            "manifest_sha256": row.manifest_sha256,
            "total_bytes": row.total_bytes,
            "state": row.state,
            "verified_nodes": sorted(
                await verified_nodes(session, row.id, row.manifest_sha256, row.total_bytes)
            ),
            "created_at": row.created_at,
        }
    )
