"""Bearer-authenticated grant control and peer-granted data-fabric streaming."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, cast

import anyio
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from opentelemetry import metrics, trace

from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingArtifactDeletionReceipt,
    TrainingArtifactGrantIssued,
    TrainingArtifactGrantRefresh,
    TrainingArtifactGrantRequest,
    TrainingArtifactImportRequest,
    TrainingArtifactImportStatus,
    TrainingArtifactManifest,
    TrainingArtifactVerificationReceipt,
    TrainingArtifactVerifyRequest,
)
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.importer import ArtifactImporter

control_router = APIRouter(prefix="/node/training/artifacts", tags=["training:artifact-control"])
data_router = APIRouter(prefix="/training-artifacts", tags=["training:artifact-data"])
tracer = trace.get_tracer("coire.node.training.artifacts")
logger = logging.getLogger(__name__)
cleanup_count = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_artifact_cleanup_total"
)


def manager(request: Request) -> TrainingArtifacts:
    return cast(TrainingArtifacts, request.app.state.training_artifacts)


def importer(request: Request) -> ArtifactImporter:
    value = getattr(request.app.state, "training_artifact_importer", None)
    if value is None:
        raise HTTPException(503, "artifact import capability unavailable")
    return cast(ArtifactImporter, value)


@control_router.post("/imports", response_model=TrainingArtifactImportStatus, status_code=202)
async def start_import(
    body: TrainingArtifactImportRequest, request: Request
) -> TrainingArtifactImportStatus:
    try:
        return await importer(request).start(body)
    except ValueError:
        raise HTTPException(409, "artifact import refused") from None


@control_router.get("/imports/{import_id}", response_model=TrainingArtifactImportStatus)
async def import_status(import_id: uuid.UUID, request: Request) -> TrainingArtifactImportStatus:
    try:
        return await anyio.to_thread.run_sync(importer(request).status, import_id)
    except ValueError:
        raise HTTPException(404, "artifact import unavailable") from None


@control_router.post(
    "/imports/{import_id}/grant", response_model=TrainingArtifactImportStatus, status_code=202
)
async def refresh_import(
    import_id: uuid.UUID, body: TrainingArtifactGrantRefresh, request: Request
) -> TrainingArtifactImportStatus:
    try:
        return await importer(request).refresh(import_id, body)
    except ValueError:
        raise HTTPException(409, "artifact import grant refresh refused") from None


@control_router.post("/grants", response_model=TrainingArtifactGrantIssued)
async def issue_grant(
    body: TrainingArtifactGrantRequest, request: Request
) -> TrainingArtifactGrantIssued:
    try:
        return await anyio.to_thread.run_sync(manager(request).issue, body)
    except (ValueError, OSError):
        raise HTTPException(409, "artifact grant refused") from None


@control_router.post("/imports/{import_id}/cancel", response_model=TrainingArtifactImportStatus)
async def cancel_import(import_id: uuid.UUID, request: Request) -> TrainingArtifactImportStatus:
    try:
        return await importer(request).cancel(import_id)
    except (ValueError, OSError):
        raise HTTPException(409, "artifact import cancellation remains unresolved") from None


@control_router.delete("/grants/{grant_id}", status_code=204)
async def revoke_grant(grant_id: uuid.UUID, request: Request) -> Response:
    manager(request).revoke(grant_id)
    return Response(status_code=204)


@control_router.post("/{artifact_id}/verify", response_model=TrainingArtifactVerificationReceipt)
async def verify_artifact(
    artifact_id: uuid.UUID, body: TrainingArtifactVerifyRequest, request: Request
) -> TrainingArtifactVerificationReceipt:
    try:
        manifest = await anyio.to_thread.run_sync(
            manager(request).verify, artifact_id, body.manifest_sha256
        )
    except (ValueError, OSError):
        raise HTTPException(409, "artifact verification refused") from None
    return TrainingArtifactVerificationReceipt(
        command_id=body.command_id,
        artifact_id=artifact_id,
        manifest_sha256=manifest.canonical_sha256(),
        node=manager(request).node_name,
        verified_bytes=manifest.total_bytes,
    )


@control_router.delete("/{artifact_id}", response_model=TrainingArtifactDeletionReceipt)
async def delete_artifact(
    artifact_id: uuid.UUID, body: TrainingArtifactDeleteRequest, request: Request
) -> TrainingArtifactDeletionReceipt:
    try:
        with tracer.start_as_current_span("coire.node.training.artifact.delete") as span:
            span.set_attribute("coire.artifact_id", str(artifact_id))
            receipt = await anyio.to_thread.run_sync(manager(request).delete, artifact_id, body)
    except (ValueError, OSError):
        cleanup_count.add(1, {"outcome": "unresolved"})
        logger.info("artifact cleanup unresolved", extra={"artifact_id": str(artifact_id)})
        raise HTTPException(409, "artifact cleanup remains unresolved") from None
    cleanup_count.add(1, {"outcome": "purged"})
    logger.info("artifact cleanup proved", extra={"artifact_id": str(artifact_id)})
    return receipt


async def authorized_manifest(
    request: Request, identity: uuid.UUID, secret: str | None, file_id: str | None = None
) -> TrainingArtifactManifest:
    if not secret or request.client is None:
        raise HTTPException(404, "artifact unavailable")
    peer = request.client.host
    try:
        value = await anyio.to_thread.run_sync(
            lambda: manager(request).authorize(secret, identity, peer, file_id=file_id)
        )
    except (ValueError, OSError):
        value = None
    if value is None:
        raise HTTPException(404, "artifact unavailable")
    return value


@data_router.get("/{artifact_id}/manifest", response_model=TrainingArtifactManifest)
async def export_manifest(
    artifact_id: uuid.UUID,
    request: Request,
    x_coire_artifact_grant: Annotated[str | None, Header()] = None,
) -> TrainingArtifactManifest:
    return await authorized_manifest(request, artifact_id, x_coire_artifact_grant)


@data_router.get("/{artifact_id}/files/{file_id}")
async def export_file(
    artifact_id: uuid.UUID,
    file_id: str,
    request: Request,
    x_coire_artifact_grant: Annotated[str | None, Header()] = None,
) -> FileResponse:
    manifest = await authorized_manifest(request, artifact_id, x_coire_artifact_grant, file_id)
    try:
        path = await anyio.to_thread.run_sync(manager(request).file, manifest, file_id)
    except (ValueError, OSError):
        raise HTTPException(404, "artifact unavailable") from None
    return FileResponse(
        path, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store"}
    )
