"""Admin-only image asset acquisition intake and immutable provenance."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import DownloadJobRow, ModelRow, ModelStateTransitionRow, NodeRow
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.registry.inspection import classify_image_inspection
from coire_api.registry.placement import NoCandidate, NodeView, choose_origin, replica_for
from coire_api.registry.service import RegistryError
from coire_core.image_assets import include_image_asset_path
from coire_core.models.audit import AuditAction, AuditOutcome
from coire_core.models.jobs import DownloadStage, JobKind, JobStatus
from coire_core.models.registry import (
    ImageAssetAcquireRequest,
    ModelKind,
    ModelState,
    Visibility,
    slug_for,
)
from coire_core.settings import Settings


def copy_manifest_valid(
    model: ModelRow,
    job: DownloadJobRow,
    status: JobStatus,
    *,
    origin_digest: str | None = None,
) -> bool:
    """Check exact inspected image bytes, immutable source and both-copy identity."""
    manifest = status.manifest
    expected = job.expected_files
    if (
        manifest is None
        or not isinstance(expected, dict)
        or not expected
        or status.job_id != job.id
        or status.slug != model.slug
        or status.kind is not (JobKind.IMPORT if origin_digest is not None else JobKind.PULL)
        or model.source_revision is None
        or manifest.slug != model.slug
        or manifest.repo_id != model.repo_id
        or manifest.revision != model.source_revision
        or status.manifest_sha256 != manifest.sha256()
        or (origin_digest is not None and status.manifest_sha256 != origin_digest)
        or len(manifest.files) != len(expected)
        or manifest.total_bytes != sum(item.bytes for item in manifest.files)
    ):
        return False
    seen: set[str] = set()
    for item in manifest.files:
        record = expected.get(item.path)
        if (
            not isinstance(record, dict)
            or item.path in seen
            or item.bytes != record.get("bytes")
            or item.upstream_sha256 != record.get("upstream_sha256")
            or (item.path.endswith(".safetensors") and item.sha256 != item.upstream_sha256)
        ):
            return False
        seen.add(item.path)
    return seen == set(expected)


async def _refuse(
    session: AsyncSession, actor: str, repo_id: str, code: str, status_code: int = 422
) -> RegistryError:
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_ADD,
        target_type="model",
        target_id=repo_id,
        outcome=AuditOutcome.REFUSED,
        detail={"reason": code},
    )
    return RegistryError(status_code, code)


async def submit_image_asset(
    session: AsyncSession,
    request: ImageAssetAcquireRequest,
    *,
    client: NodeClient,
    settings: Settings,
    views: list[NodeView],
    actor: str,
) -> tuple[ModelRow, DownloadJobRow]:
    """Inspect metadata and the reviewed licence before making a pull job."""
    existing = (
        await session.execute(select(ModelRow).where(ModelRow.repo_id == request.repo_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise await _refuse(session, actor, request.repo_id, "duplicate", 409)
    try:
        origin = choose_origin(views)
        replica = replica_for(origin, views)
    except NoCandidate as exc:
        raise await _refuse(session, actor, request.repo_id, "nodes_unavailable", 503) from exc
    try:
        inspection = await client.inspect(origin.name, request.repo_id)
    except NodeError as exc:
        raise await _refuse(session, actor, request.repo_id, "inspection_failed", 503) from exc
    result = classify_image_inspection(inspection, request.kind)
    if not result.supported:
        raise await _refuse(session, actor, request.repo_id, result.rejection_code or "unsupported")
    if result.license_id != request.accepted_license_id:
        raise await _refuse(session, actor, request.repo_id, "licence_review_mismatch")
    selected = {
        item.path
        for item in inspection.files
        if include_image_asset_path(request.repo_id, request.kind, item.path)
        and (
            item.path.endswith(
                (".safetensors", ".json", ".txt", ".model", ".tiktoken", ".md", ".yaml", ".yml")
            )
            or item.path in {".gitattributes", "LICENSE", "LICENSE.txt"}
        )
    }
    selected_total = sum(item.bytes for item in inspection.files if item.path in selected)
    selected_weights = sum(
        item.bytes
        for item in inspection.files
        if item.path in selected and item.path.endswith(".safetensors")
    )
    # Native image execution may use much more unified memory than its weights
    # occupy on disk. The local tiny-model smoke measured 13.6 GiB of transient
    # physical footprint; leave a 16 GiB floor above the file-based estimate.
    memory_estimate = max(selected_total * 2, selected_weights * 3)
    if request.kind is ModelKind.IMAGE_MODEL:
        memory_estimate += 16 * 1024**3
    if any(
        view.store_free_bytes < selected_total + settings.disk_reserve_bytes
        for view in (origin, replica)
    ):
        raise await _refuse(session, actor, request.repo_id, "insufficient_disk")
    if any(view.memory_free_bytes < memory_estimate for view in (origin, replica)):
        raise await _refuse(session, actor, request.repo_id, "insufficient_memory")
    nodes = (
        (
            await session.execute(
                select(NodeRow).where(NodeRow.name.in_((origin.name, replica.name)))
            )
        )
        .scalars()
        .all()
    )
    by_name = {node.name: node for node in nodes}
    if origin.name not in by_name or replica.name not in by_name:
        raise await _refuse(session, actor, request.repo_id, "nodes_unregistered", 503)
    model = ModelRow(
        id=uuid.uuid4(),
        kind=request.kind,
        backend=result.backend.value,
        source="studio",
        repo_id=request.repo_id,
        slug=slug_for(request.repo_id),
        display_name=request.display_name or request.repo_id.rsplit("/", 1)[-1],
        description=request.description,
        state=ModelState.DOWNLOADING,
        visibility=Visibility.ADMIN_ONLY,
        entitlement=[],
        tags=[],
        placement_policy=request.placement_policy,
        precision="safetensors",
        weight_bytes=selected_weights,
        total_bytes=selected_total,
        file_count=len(selected),
        memory_estimate_bytes=memory_estimate,
        capability_profile={},
        image_capability_profile=None,
        source_revision=inspection.revision,
        license_id=inspection.license_id,
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelStateTransitionRow(
            model_id=model.id,
            from_state=None,
            to_state=ModelState.DOWNLOADING,
            reason=f"image asset admitted by {actor}",
        )
    )
    job = DownloadJobRow(
        id=uuid.uuid4(),
        model_id=model.id,
        origin_node_id=by_name[origin.name].id,
        replica_node_id=by_name[replica.name].id,
        stage=DownloadStage.PULL,
        bytes_total=model.total_bytes,
        files_total=model.file_count,
        expected_files={
            item.path: {"bytes": item.bytes, "upstream_sha256": item.upstream_sha256}
            for item in inspection.files
            if item.path in selected
        },
    )
    session.add(job)
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_ADD,
        target_type="model",
        target_id=str(model.id),
        detail={
            "repo_id": request.repo_id,
            "kind": request.kind.value,
            "revision": inspection.revision,
            "license_id": inspection.license_id,
            "origin": origin.name,
            "replica": replica.name,
        },
    )
    return model, job
