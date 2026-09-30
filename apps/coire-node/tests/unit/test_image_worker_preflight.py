"""Only an exact safe local image copy may reach the bare worker."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_node.image_runtime.preflight import ImageCopyUnavailable, verify_image_copy
from coire_node.store import Store

SLUG = "studio--z-image-turbo"


def _copy(tmp_path: Path) -> tuple[Store, ImageWorkerLoadRequest]:
    store = Store(tmp_path)
    root = store.path_for(SLUG)
    root.mkdir()
    (root / "config.json").write_text("{}")
    (root / "model.safetensors").write_bytes(b"safe")
    manifest = store.hash_tree(SLUG, repo_id="studio/z-image-turbo", revision="v1")
    store.write_manifest(manifest)
    request = ImageWorkerLoadRequest(
        slug=SLUG,
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256=manifest.sha256(),
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    return store, request


def test_verified_copy_returns_only_store_path(tmp_path: Path) -> None:
    store, request = _copy(tmp_path)
    assert verify_image_copy(store, request) == store.path_for(SLUG)


@pytest.mark.parametrize(
    "change",
    [
        "wrong_digest",
        "wrong_runtime",
        "missing",
        "tampered",
        "extra",
        "unsafe",
        "symlink",
        "directory_symlink",
        "manifest_symlink",
    ],
)
def test_refuses_missing_tampered_or_unsafe_copy(tmp_path: Path, change: str) -> None:
    store, request = _copy(tmp_path)
    root = store.path_for(SLUG)
    if change == "wrong_digest":
        request = request.model_copy(update={"manifest_sha256": "0" * 64})
    elif change == "wrong_runtime":
        request = request.model_copy(update={"runtime_version": "mflux-0.21.0"})
    elif change == "missing":
        store.manifest_path(SLUG).unlink()
    elif change == "tampered":
        (root / "config.json").write_text('{"changed":true}')
    elif change == "extra":
        (root / "extra.json").write_text("{}")
    elif change == "unsafe":
        (root / "evil.py").write_text("pass")
    elif change == "symlink":
        (root / "linked.json").symlink_to(root / "config.json")
    elif change == "directory_symlink":
        (root / "linked_dir").symlink_to(root, target_is_directory=True)
    elif change == "manifest_symlink":
        manifest_path = store.manifest_path(SLUG)
        saved = tmp_path / "saved.json"
        saved.write_bytes(manifest_path.read_bytes())
        manifest_path.unlink()
        manifest_path.symlink_to(saved)
    with pytest.raises(ImageCopyUnavailable):
        verify_image_copy(store, request)
