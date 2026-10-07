"""Bearer control metadata/imports; peer-granted component bytes only on data fabric."""

from __future__ import annotations

import uuid
from typing import Annotated, cast

import anyio
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    NodeTrainingStatus,
    TrainingArtifactGrantIssued,
    TrainingArtifactGrantRefresh,
    TrainingArtifactVerifyRequest,
    TrainingRankCollection,
    TrainingRankComponentManifest,
    TrainingRankGrantRequest,
    TrainingRankImportRequest,
    TrainingRankImportStatus,
    TrainingRankVerificationReceipt,
)
from coire_core.models.training_types import TrainingId
from coire_node.routes.training import invoke, native, scope
from coire_node.training.checkpoints import _private_file
from coire_node.training.components import RankImporter, TrainingComponents

control_router = APIRouter(prefix="/node/training", tags=["training:component-control"])
data_router = APIRouter(prefix="/training-components", tags=["training:component-data"])


def manager(request: Request) -> TrainingComponents:
    value = getattr(request.app.state, "training_components", None)
    if value is None:
        raise HTTPException(503, "rank component capability unavailable")
    return cast(TrainingComponents, value)


def importer(request: Request) -> RankImporter:
    return cast(RankImporter, request.app.state.training_rank_importer)


@control_router.post("/attempts/{attempt_id}/rank-collection", response_model=NodeTrainingStatus)
async def collection(
    attempt_id: TrainingId, body: TrainingRankCollection, request: Request
) -> NodeTrainingStatus:
    scope(attempt_id, body, request)
    supervisor = native(request)
    await invoke(lambda: supervisor.publish_collection(body))
    return supervisor.observe(attempt_id)


@control_router.get(
    "/components/{artifact_id}/ranks/{rank}", response_model=TrainingRankComponentManifest
)
async def metadata(
    artifact_id: uuid.UUID, rank: int, request: Request
) -> TrainingRankComponentManifest:
    try:
        value = await anyio.to_thread.run_sync(manager(request).manifest, artifact_id, rank)
        manager(request).scope(value)
        return value
    except (ValueError, OSError, TrainingConflict, TrainingValidationError):
        raise HTTPException(404, "rank component unavailable") from None


@control_router.post(
    "/components/{artifact_id}/ranks/{rank}/verify", response_model=TrainingRankVerificationReceipt
)
async def verify(
    artifact_id: uuid.UUID, rank: int, body: TrainingArtifactVerifyRequest, request: Request
) -> TrainingRankVerificationReceipt:
    try:
        value = await anyio.to_thread.run_sync(manager(request).manifest, artifact_id, rank)
        if value.canonical_sha256() != body.manifest_sha256:
            raise TrainingConflict("Rank descriptor differs")
        await anyio.to_thread.run_sync(manager(request).verify, value)
        return TrainingRankVerificationReceipt(
            command_id=body.command_id,
            component=value,
            node=manager(request).node_name,
            verified_bytes=value.total_bytes,
        )
    except (ValueError, OSError, TrainingConflict, TrainingValidationError):
        raise HTTPException(409, "rank component verification refused") from None


@control_router.post("/components/grants", response_model=TrainingArtifactGrantIssued)
async def grant(body: TrainingRankGrantRequest, request: Request) -> TrainingArtifactGrantIssued:
    return await invoke(lambda: anyio.to_thread.run_sync(manager(request).issue, body))


@control_router.delete("/components/grants/{grant_id}", status_code=204)
async def revoke(grant_id: uuid.UUID, request: Request) -> Response:
    manager(request).revoke(grant_id)
    return Response(status_code=204)


@control_router.post(
    "/components/imports", response_model=TrainingRankImportStatus, status_code=202
)
async def start(body: TrainingRankImportRequest, request: Request) -> TrainingRankImportStatus:
    return await invoke(lambda: importer(request).start(body))


@control_router.get("/components/imports/{import_id}", response_model=TrainingRankImportStatus)
async def status(import_id: uuid.UUID, request: Request) -> TrainingRankImportStatus:
    try:
        return await anyio.to_thread.run_sync(importer(request).status, import_id)
    except (ValueError, OSError, TrainingConflict, TrainingValidationError):
        raise HTTPException(404, "rank import unavailable") from None


@control_router.post(
    "/components/imports/{import_id}/grant",
    response_model=TrainingRankImportStatus,
    status_code=202,
)
async def refresh(
    import_id: uuid.UUID, body: TrainingArtifactGrantRefresh, request: Request
) -> TrainingRankImportStatus:
    return await invoke(lambda: importer(request).refresh(import_id, body))


@control_router.post(
    "/components/imports/{import_id}/cancel", response_model=TrainingRankImportStatus
)
async def cancel(import_id: uuid.UUID, request: Request) -> TrainingRankImportStatus:
    return await invoke(lambda: importer(request).cancel(import_id))


async def authorized(
    request: Request, identity: uuid.UUID, rank: int, secret: str | None, file_id: str | None = None
) -> TrainingRankComponentManifest:
    if not secret or request.client is None:
        raise HTTPException(404, "rank component unavailable")
    try:
        value = await anyio.to_thread.run_sync(
            manager(request).authorize, secret, identity, rank, request.client.host, file_id
        )
        if value is not None:
            return value
    except (ValueError, OSError, TrainingConflict, TrainingValidationError):
        pass
    raise HTTPException(404, "rank component unavailable")


@data_router.get(
    "/{artifact_id}/ranks/{rank}/manifest", response_model=TrainingRankComponentManifest
)
async def export_manifest(
    artifact_id: uuid.UUID,
    rank: int,
    request: Request,
    x_coire_artifact_grant: Annotated[str | None, Header()] = None,
) -> TrainingRankComponentManifest:
    return await authorized(request, artifact_id, rank, x_coire_artifact_grant)


@data_router.get("/{artifact_id}/ranks/{rank}/files/{file_id}")
async def export_file(
    artifact_id: uuid.UUID,
    rank: int,
    file_id: str,
    request: Request,
    x_coire_artifact_grant: Annotated[str | None, Header()] = None,
) -> FileResponse:
    manifest = await authorized(request, artifact_id, rank, x_coire_artifact_grant, file_id)
    file = next(f for f in manifest.files if f.id == file_id)
    path = manager(request).directory(artifact_id, rank) / file.name
    try:
        await anyio.to_thread.run_sync(_private_file, path)
        if path.stat().st_size != file.bytes:
            raise TrainingConflict("Rank file size differs")
    except (ValueError, OSError, TrainingConflict, TrainingValidationError):
        raise HTTPException(404, "rank component unavailable") from None
    return FileResponse(
        path, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store"}
    )
