"""Training artifact grants are immutable, peer-bound and data-listener-only."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coire_core.models.node import NetworkPath
from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingArtifactFile,
    TrainingArtifactGrantRequest,
    TrainingArtifactManifest,
)
from coire_node.testing.harness import TOKEN, Agent
from coire_node.training.artifacts import TrainingArtifacts

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def artifact(root: Path) -> TrainingArtifactManifest:
    identity = uuid.uuid4()
    directory = root / str(identity)
    directory.mkdir(mode=0o700, parents=True)
    data = b"synthetic-artifact-bytes"
    file = directory / "adapter.safetensors"
    file.write_bytes(data)
    file.chmod(0o600)
    manifest = TrainingArtifactManifest(
        artifact_id=identity,
        kind="adapter",
        files=[
            TrainingArtifactFile(
                id="adapter",
                name=file.name,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        ],
        total_bytes=len(data),
    )
    path = directory / "manifest.json"
    path.write_text(manifest.model_dump_json())
    path.chmod(0o600)
    return manifest


def grant(manifest: TrainingArtifactManifest) -> TrainingArtifactGrantRequest:
    return TrainingArtifactGrantRequest(
        command_id=uuid.uuid4(),
        artifact_id=manifest.artifact_id,
        manifest_sha256=manifest.canonical_sha256(),
        source_node="coire-edge-a",
        destination_node="coire-edge-b",
        attempt_id=JOB,
        fence=1,
        file_ids=["adapter"],
        max_bytes=manifest.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )


def test_source_grant_scopes_bytes_manifest_peer_and_single_revoke(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    manifest = artifact(root)
    manager = TrainingArtifacts(
        root, node_name="coire-edge-a", peer_addresses=lambda _peer: {"testclient"}
    )
    request = grant(manifest)
    issued = manager.issue(request)
    assert manager.issue(request).grant_id == issued.grant_id
    assert manager.authorize(issued.secret, manifest.artifact_id, "testclient", file_id="adapter")
    assert manager.authorize(issued.secret, uuid.uuid4(), "testclient", file_id="adapter") is None
    assert (
        manager.authorize(issued.secret, manifest.artifact_id, "other-client", file_id="adapter")
        is None
    )
    assert (
        manager.authorize(issued.secret, manifest.artifact_id, "testclient", file_id="other-file")
        is None
    )
    other = manager.issue(grant(manifest))
    manager.revoke(issued.grant_id)
    assert manager.authorize(issued.secret, manifest.artifact_id, "testclient") is None
    assert manager.authorize(other.secret, manifest.artifact_id, "testclient")


def test_expired_or_changed_manifest_grants_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    manifest = artifact(root)
    manager = TrainingArtifacts(
        root, node_name="coire-edge-a", peer_addresses=lambda _peer: {"testclient"}
    )
    with pytest.raises(ValueError):
        manager.issue(
            grant(manifest).model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        )
    with pytest.raises(ValueError):
        manager.issue(grant(manifest).model_copy(update={"manifest_sha256": "f" * 64}))


def test_artifact_reads_only_exist_on_data_listener_and_support_range(tmp_path: Path) -> None:
    agent = Agent(tmp_path / "node")
    root = tmp_path / "artifacts"
    manifest = artifact(root)
    manager = TrainingArtifacts(
        root, node_name="coire-edge-a", peer_addresses=lambda _peer: {"testclient"}
    )
    try:
        control = agent.app(NetworkPath.CONTROL)
        data = agent.app(NetworkPath.DATA)
        manager.attach(control, data)
        with TestClient(control) as client:
            path = f"/training-artifacts/{manifest.artifact_id}/manifest"
            assert client.get(path).status_code == 404
            assert (
                client.post(
                    "/node/training/artifacts/grants", json=grant(manifest).model_dump(mode="json")
                ).status_code
                == 401
            )
            issued = client.post(
                "/node/training/artifacts/grants",
                headers={"Authorization": f"Bearer {TOKEN}"},
                json=grant(manifest).model_dump(mode="json"),
            )
            assert issued.status_code == 200
        with TestClient(data) as client:
            assert client.get(path).status_code == 404
            headers = {"X-Coire-Artifact-Grant": issued.json()["secret"]}
            assert client.get(path, headers=headers).status_code == 200
            output = client.get(
                f"/training-artifacts/{manifest.artifact_id}/files/adapter",
                headers={**headers, "Range": "bytes=0-3"},
            )
            assert output.status_code == 206 and output.content == b"synt"
    finally:
        agent.close()


def test_cleanup_is_authenticated_control_only_and_available_when_disabled(tmp_path: Path) -> None:
    agent = Agent(tmp_path / "node", training_enabled=False)
    root = tmp_path / "artifacts"
    manifest = artifact(root)
    manager = TrainingArtifacts(root, node_name="coire-edge-a", referenced=lambda _: False)
    command = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=manifest.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
    )
    path = f"/node/training/artifacts/{manifest.artifact_id}"
    try:
        control, data = agent.app(NetworkPath.CONTROL), agent.app(NetworkPath.DATA)
        manager.attach(control, data)
        with TestClient(data) as client:
            assert (
                client.request("DELETE", path, json=command.model_dump(mode="json")).status_code
                == 404
            )
        with TestClient(control) as client:
            assert (
                client.request("DELETE", path, json=command.model_dump(mode="json")).status_code
                == 401
            )
            headers = {"Authorization": f"Bearer {TOKEN}"}
            first = client.request(
                "DELETE", path, headers=headers, json=command.model_dump(mode="json")
            )
            assert first.status_code == 200 and first.json()["purged"]
            assert (
                client.request(
                    "DELETE", path, headers=headers, json=command.model_dump(mode="json")
                ).json()
                == first.json()
            )
    finally:
        agent.close()
