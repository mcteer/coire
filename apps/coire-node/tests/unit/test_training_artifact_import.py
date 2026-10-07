"""Artifact transfer verifies complete batches, journals no secrets and refreshes lost grants."""

import asyncio
import hashlib
import uuid
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

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def request(manifest: TrainingArtifactManifest) -> TrainingArtifactImportRequest:
    return TrainingArtifactImportRequest(
        command_id=uuid.uuid4(),
        artifact_id=manifest.artifact_id,
        manifest_sha256=manifest.canonical_sha256(),
        source_node="coire-edge-a",
        destination_node="coire-edge-b",
        attempt_id=JOB,
        fence=1,
        grant_id=uuid.uuid4(),
        grant_secret="test-transfer-secret-" + "x" * 32,
    )


async def terminal(importer: ArtifactImporter, identity: uuid.UUID) -> str:
    for _ in range(100):
        state = importer.status(identity).state
        if state in {"verified", "failed", "cancelled"}:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("artifact import did not reach a bounded terminal observation")


async def test_complete_artifact_and_restart_observation_keep_credentials_out_of_journal(
    tmp_path: Path,
) -> None:
    data = b"synthetic-checkpoint-transfer"
    identity = uuid.uuid4()
    manifest = TrainingArtifactManifest(
        artifact_id=identity,
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
    command = request(manifest)

    def respond(message: httpx.Request) -> httpx.Response:
        assert message.url.host == "coire-edge-a.fabric"
        assert message.headers["X-Coire-Artifact-Grant"] == command.grant_secret
        if message.url.path.endswith("/manifest"):
            return httpx.Response(200, json=manifest.model_dump(mode="json"))
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        artifacts = TrainingArtifacts(tmp_path / "artifacts", node_name="coire-edge-b")
        importer = ArtifactImporter(
            artifacts,
            tmp_path / "journal",
            port=9401,
            client=DataFabricClient(client=client),
            disk_floor_bytes=0,
        )
        await importer.start(command)
        assert await terminal(importer, command.command_id) == "verified"
        assert artifacts.manifest(identity).canonical_sha256() == manifest.canonical_sha256()
        assert (
            command.grant_secret
            not in (tmp_path / "journal" / f"{command.command_id}.json").read_text()
        )
        await importer.aclose()
        restarted = ArtifactImporter(
            artifacts,
            tmp_path / "journal",
            port=9401,
            client=DataFabricClient(client=client),
            disk_floor_bytes=0,
        )
        assert restarted.status(command.command_id).state == "verified"
        assert (await restarted.start(command)).state == "verified"
        await restarted.aclose()


async def test_expired_grant_refresh_resumes_same_intent_and_checksum_failure_stays_private(
    tmp_path: Path,
) -> None:
    data = b"verified-bytes"
    identity = uuid.uuid4()
    manifest = TrainingArtifactManifest(
        artifact_id=identity,
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
    command = request(manifest)
    allow = False

    def respond(message: httpx.Request) -> httpx.Response:
        if not allow:
            return httpx.Response(404)
        if message.url.path.endswith("/manifest"):
            return httpx.Response(200, json=manifest.model_dump(mode="json"))
        return httpx.Response(200, content=b"invalid-bytes!")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        artifacts = TrainingArtifacts(tmp_path / "artifacts", node_name="coire-edge-b")
        importer = ArtifactImporter(
            artifacts,
            tmp_path / "journal",
            port=9401,
            client=DataFabricClient(client=client),
            disk_floor_bytes=0,
        )
        await importer.start(command)
        assert await terminal(importer, command.command_id) == "failed"
        assert importer.status(command.command_id).reason == "lease_expired"
        allow = True
        resumed = await importer.refresh(
            command.command_id,
            TrainingArtifactGrantRefresh(
                command_id=uuid.uuid4(),
                artifact_id=identity,
                manifest_sha256=manifest.canonical_sha256(),
                attempt_id=JOB,
                fence=1,
                grant_id=uuid.uuid4(),
                grant_secret="fresh-transfer-secret-" + "y" * 32,
            ),
        )
        assert resumed.state == "staging" and resumed.reason is None
        assert await terminal(importer, command.command_id) == "failed"
        assert importer.status(command.command_id).reason == "replication_failed"
        assert not (tmp_path / "artifacts" / str(identity)).exists()
        await importer.aclose()
