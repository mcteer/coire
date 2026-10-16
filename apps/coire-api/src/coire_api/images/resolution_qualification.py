"""Audited resolution qualification on already acquired, immutable image copies."""

from __future__ import annotations

import uuid

from opentelemetry import metrics, trace
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import DownloadJobRow, ModelCopyRow, ModelRow, NodeRow
from coire_api.registry.placement import NodeView
from coire_core.errors import ImageConflict, ImageNotFound
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageMode,
    ImageResolutionQualificationReceipt,
    ImageResolutionQualificationRequest,
)
from coire_core.models.jobs import DownloadStage
from coire_core.models.registry import ModelKind, ModelState

tracer = trace.get_tracer("coire.api.images.resolution")
qualification_total = metrics.get_meter("coire.api.images.resolution").create_counter(
    "coire_image_resolution_qualification_total",
    unit="1",
    description="Image resolution qualification outcomes",
)


def is_qualification(job: DownloadJobRow) -> bool:
    return (job.image_validation or {}).get("resolution_qualification") is True


def qualification_request(job: DownloadJobRow) -> ImageResolutionQualificationRequest | None:
    if not is_qualification(job):
        return None
    return ImageResolutionQualificationRequest.model_validate(
        (job.image_validation or {}).get("request")
    )


async def submit_qualification(
    session: AsyncSession,
    model_id: uuid.UUID,
    request: ImageResolutionQualificationRequest,
    *,
    principal: Principal,
    views: list[NodeView],
) -> ImageResolutionQualificationReceipt:
    with tracer.start_as_current_span("coire.api.images.resolution.qualify") as span:
        span.set_attribute("model_id", str(model_id))
        model = await session.scalar(
            select(ModelRow).where(ModelRow.id == model_id).with_for_update()
        )
        if model is None:
            raise ImageNotFound()
        if (
            model.kind != ModelKind.IMAGE_MODEL
            or model.state != ModelState.READY
            or not model.image_capability_profile
            or not model.manifest_sha256
            or not model.source_revision
            or ImageMode.TXT2IMG
            not in ImageCapabilityProfile.model_validate(model.image_capability_profile).modes
        ):
            raise ImageConflict("A ready validated txt2img base is required")
        pending = await session.scalar(
            select(DownloadJobRow.id)
            .where(
                DownloadJobRow.model_id == model.id,
                or_(
                    DownloadJobRow.stage.notin_([DownloadStage.DONE, DownloadStage.FAILED]),
                    DownloadJobRow.image_validation["reservation_cleanup_pending"]
                    .as_boolean()
                    .is_(True),
                ),
            )
            .limit(1)
        )
        if pending is not None:
            raise ImageConflict("Model qualification or acquisition is already running")
        copies = (
            await session.scalars(
                select(ModelCopyRow)
                .where(
                    ModelCopyRow.model_id == model.id,
                    ModelCopyRow.verified.is_(True),
                    ModelCopyRow.manifest_sha256 == model.manifest_sha256,
                )
                .order_by(ModelCopyRow.role)
            )
        ).all()
        if len(copies) != 2 or copies[0].node_id == copies[1].node_id:
            raise ImageConflict("Both pinned model copies must be verified")
        nodes = (
            await session.scalars(
                select(NodeRow).where(NodeRow.id.in_([c.node_id for c in copies]))
            )
        ).all()
        live = {view.name: view for view in views}
        if len(nodes) != 2 or any(
            node.name not in live
            or not live[node.name].healthy
            or live[node.name].memory_free_bytes < request.reservation_bytes
            for node in nodes
        ):
            raise ImageConflict("Both Studios need available qualification memory")
        job = DownloadJobRow(
            id=uuid.uuid4(),
            model_id=model.id,
            origin_node_id=copies[0].node_id,
            replica_node_id=copies[1].node_id,
            stage=DownloadStage.VERIFY_REPLICA,
            image_validation={
                "resolution_qualification": True,
                "request": request.model_dump(mode="json"),
                "previous_profile": model.image_capability_profile,
                "previous_memory": model.memory_estimate_bytes,
                "node_names": [
                    next(n.name for n in nodes if n.id == copy.node_id) for copy in copies
                ],
                "reservation_cleanup_pending": True,
            },
        )
        session.add(job)
        await write_principal_audit(
            session,
            principal=principal,
            action="image.resolution.qualification_requested",
            target_type="model",
            target_id=str(model.id),
            detail={"job_id": str(job.id), **request.model_dump(mode="json")},
        )
        qualification_total.add(1, {"outcome": "queued"})
        return ImageResolutionQualificationReceipt(model_id=model.id, job_id=job.id)
