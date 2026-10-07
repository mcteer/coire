"""A lost connection resumes only its verified artifact scope and exact remaining Range."""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from coire_core.models.training_node import (
    TrainingArtifactFile,
    TrainingArtifactGrantRefresh,
    TrainingArtifactImportRequest,
    TrainingArtifactManifest,
)
from coire_core.net import DataFabricClient
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.importer import ArtifactImporter


class Interrupted(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"x" * (1024**2)
        raise httpx.ReadError("simulated link interruption")


async def test_range_refresh_completes_without_overwriting_a_different_artifact(
    tmp_path: Path,
) -> None:
    data = b"x" * (1024**2) + b"suffix"
    artifact_id = uuid.uuid4()
    manifest = TrainingArtifactManifest(
        artifact_id=artifact_id,
        kind="adapter",
        files=[
            TrainingArtifactFile(
                id="adapter",
                name="adapter.safetensors",
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        ],
        total_bytes=len(data),
    )
    command = TrainingArtifactImportRequest(
        command_id=uuid.uuid4(),
        artifact_id=artifact_id,
        manifest_sha256=manifest.canonical_sha256(),
        source_node="coire-edge-a",
        destination_node="coire-edge-b",
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        fence=1,
        grant_id=uuid.uuid4(),
        grant_secret="original-" + "x" * 32,
    )
    resumed = False

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, json=manifest.model_dump(mode="json"))
        if not resumed:
            return httpx.Response(200, stream=Interrupted())
        assert request.headers["Range"] == f"bytes={1024**2}-"
        return httpx.Response(
            206,
            content=b"suffix",
            headers={"Content-Range": f"bytes {1024**2}-{len(data) - 1}/{len(data)}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        store = TrainingArtifacts(tmp_path / "artifacts", node_name="coire-edge-b")
        importer = ArtifactImporter(
            store,
            tmp_path / "journals",
            port=9401,
            client=DataFabricClient(client=client),
            disk_floor_bytes=0,
        )
        await importer.start(command)
        for _ in range(100):
            if importer.status(command.command_id).state == "failed":
                break
            await asyncio.sleep(0.01)
        assert importer.status(command.command_id).state == "failed"
        partial = tmp_path / "artifacts" / f".import-{artifact_id}" / "adapter.safetensors"
        assert partial.stat().st_size == 1024**2 and partial.stat().st_mode & 0o777 == 0o600
        resumed = True
        await importer.refresh(
            command.command_id,
            TrainingArtifactGrantRefresh(
                command_id=uuid.uuid4(),
                artifact_id=artifact_id,
                manifest_sha256=manifest.canonical_sha256(),
                attempt_id=command.attempt_id,
                fence=1,
                grant_id=uuid.uuid4(),
                grant_secret="renewed-" + "y" * 32,
            ),
        )
        for _ in range(100):
            if importer.status(command.command_id).state == "verified":
                break
            await asyncio.sleep(0.01)
        assert importer.status(command.command_id).state == "verified"
        assert (
            tmp_path / "artifacts" / str(artifact_id) / "adapter.safetensors"
        ).read_bytes() == data
        await importer.aclose()
