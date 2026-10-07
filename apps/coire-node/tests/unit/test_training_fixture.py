"""The training fixture cannot accidentally run model work on core or download assets."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.models.jobs import ChecksumManifest, ManifestFile
from coire_node.testing.training import offline_training_model


def test_training_fixture_refuses_core_before_model_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coire_node.testing.training.platform.node", lambda: "coire-core.lab")
    with pytest.raises(ValueError, match="cannot execute on core"):
        offline_training_model("/nonexistent")


@pytest.mark.parametrize("value", [None, "hf-org/model", "/nonexistent"])
def test_training_fixture_requires_a_local_asset(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.setattr("coire_node.testing.training.platform.node", lambda: "isolated-test-mac")
    monkeypatch.setattr("coire_node.testing.training.platform.system", lambda: "Darwin")
    monkeypatch.setattr("coire_node.testing.training.platform.machine", lambda: "arm64")
    with pytest.raises(ValueError, match=r"local|absolute"):
        offline_training_model(value)


def test_training_fixture_requires_acquisition_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("coire_node.testing.training.platform.node", lambda: "isolated-test-mac")
    monkeypatch.setattr("coire_node.testing.training.platform.system", lambda: "Darwin")
    monkeypatch.setattr("coire_node.testing.training.platform.machine", lambda: "arm64")
    with pytest.raises(ValueError, match="acquisition checksum manifest"):
        offline_training_model(str(tmp_path))


def test_training_fixture_verifies_whole_tree_without_importing_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("coire_node.testing.training.platform.node", lambda: "isolated-test-mac")
    monkeypatch.setattr("coire_node.testing.training.platform.system", lambda: "Darwin")
    monkeypatch.setattr("coire_node.testing.training.platform.machine", lambda: "arm64")
    root = tmp_path / "synthetic--metadata-only"
    root.mkdir()
    payloads = {
        "config.json": b'{"model_type":"llama"}',
        "model.safetensors": b"not-real-model-data",
    }
    files = []
    for name, data in payloads.items():
        (root / name).write_bytes(data)
        files.append(
            ManifestFile(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        )
    manifest = ChecksumManifest(
        slug=root.name,
        repo_id="synthetic/metadata-only",
        revision="test",
        files=files,
        total_bytes=sum(len(data) for data in payloads.values()),
        created_at=datetime.now(UTC),
    )
    root.with_name(root.name + ".manifest.json").write_text(manifest.model_dump_json())
    assert offline_training_model(str(root)) == root
    (root / "extra.safetensors").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unmanifested"):
        offline_training_model(str(root))
