"""Serving identities inspect acquired inert bytes without loading a tokenizer."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.models.jobs import ChecksumManifest, ManifestFile
from coire_node.rendering_identity import inspect_rendering_identity


def manifest_for(root: Path) -> ChecksumManifest:
    files = [
        ManifestFile(
            path=path.name,
            bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.iterdir())
    ]
    return ChecksumManifest(
        slug="tiny",
        repo_id="test/tiny",
        revision="test",
        files=files,
        total_bytes=sum(item.bytes for item in files),
        created_at=datetime.now(UTC),
    )


def test_identity_matches_analysis_hashes_and_file_template_priority(tmp_path: Path) -> None:
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": "inline"}))
    (tmp_path / "tokenizer.json").write_text("{}")
    (tmp_path / "chat_template.jinja").write_text("file-template")
    manifest = manifest_for(tmp_path)
    versions = dict.fromkeys(("mlx", "mlx-lm", "transformers", "tokenizers"), "test")
    identity = inspect_rendering_identity(tmp_path, manifest, versions=versions)
    assert identity is not None
    expected = sorted(
        (item.path, item.sha256) for item in manifest.files if item.path.startswith("tokenizer")
    )
    assert (
        identity.tokenizer_sha256
        == hashlib.sha256(json.dumps(expected, separators=(",", ":")).encode()).hexdigest()
    )
    assert identity.template_sha256 == hashlib.sha256(b"file-template").hexdigest()
    assert (
        identity.runtime_sha256
        == hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
    )
    override = inspect_rendering_identity(
        tmp_path, manifest, template_override="override", versions=versions
    )
    assert override is not None
    assert override.template_sha256 == hashlib.sha256(b"override").hexdigest()


@pytest.mark.parametrize("change", ["checksum", "link", "unacquired", "ambiguous"])
def test_unverifiable_rendering_has_no_identity(tmp_path: Path, change: str) -> None:
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": "inline"}))
    (tmp_path / "tokenizer.json").write_text("{}")
    if change == "ambiguous":
        (tmp_path / "tokenizer_config.json").write_text(
            json.dumps({"chat_template": {"first": "a", "second": "b"}})
        )
    manifest = manifest_for(tmp_path)
    if change == "checksum":
        (tmp_path / "tokenizer.json").write_text("changed")
    elif change == "link":
        (tmp_path / "tokenizer.json").unlink()
        (tmp_path / "tokenizer.json").symlink_to(tmp_path / "tokenizer_config.json")
    elif change == "unacquired":
        (tmp_path / "chat_template.jinja").write_text("unacquired")
    assert (
        inspect_rendering_identity(
            tmp_path,
            manifest,
            versions=dict.fromkeys(("mlx", "mlx-lm", "transformers", "tokenizers"), "test"),
        )
        is None
    )
