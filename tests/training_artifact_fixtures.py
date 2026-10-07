"""Small inert private artifact/grant fixtures shared by independent test selections."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coire_core.models.training_node import (
    TrainingArtifactFile,
    TrainingArtifactGrantRequest,
    TrainingArtifactManifest,
)


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
        total_bytes=len(data),
        files=[
            TrainingArtifactFile(
                id="adapter",
                name=file.name,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        ],
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
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        fence=1,
        file_ids=[file.id for file in manifest.files],
        max_bytes=manifest.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
