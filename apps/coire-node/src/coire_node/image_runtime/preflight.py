"""Exact local-copy gate before starting a bare mflux image worker."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_node.hub import _IMAGE_SAFE_NAMES, _IMAGE_SAFE_SUFFIXES
from coire_node.store import Store, StoreError

RUNTIME_VERSION = "mflux-0.20.0"


class ImageCopyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("image copy unavailable")


def _safe_local_tree(root: Path) -> None:
    def fail_walk(exc: OSError) -> None:
        raise exc

    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ImageCopyUnavailable()
        for parent, directories, filenames in os.walk(root, followlinks=False, onerror=fail_walk):
            current = Path(parent)
            for name in (*directories, *filenames):
                child = current / name
                mode = child.lstat().st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise ImageCopyUnavailable()
                if stat.S_ISREG(mode):
                    relative = child.relative_to(root)
                    if relative.parts[0] == ".cache":
                        continue
                    if (
                        child.name not in _IMAGE_SAFE_NAMES
                        and child.suffix not in _IMAGE_SAFE_SUFFIXES
                    ):
                        raise ImageCopyUnavailable()
    except (OSError, ValueError) as exc:
        raise ImageCopyUnavailable() from exc


def verify_image_copy(store: Store, request: ImageWorkerLoadRequest) -> Path:
    """Return only the exact verified Store path; never acquire missing bytes."""
    if request.runtime_version != RUNTIME_VERSION:
        raise ImageCopyUnavailable()
    try:
        raw_path = store.root / request.slug
        if not stat.S_ISDIR(raw_path.lstat().st_mode):
            raise ImageCopyUnavailable()
        root = store.path_for(request.slug)
        manifest_path = store.manifest_path(request.slug)
        if not stat.S_ISREG(manifest_path.lstat().st_mode):
            raise ImageCopyUnavailable()
        manifest = store.read_manifest(request.slug)
        if (
            manifest is None
            or manifest.slug != request.slug
            or manifest.sha256() != request.manifest_sha256
            or not manifest.files
            or sum(entry.bytes for entry in manifest.files) != manifest.total_bytes
        ):
            raise ImageCopyUnavailable()
        _safe_local_tree(root)
        if store.verify_against(request.slug, manifest):
            raise ImageCopyUnavailable()
        return root
    except (OSError, StoreError, ValueError) as exc:
        raise ImageCopyUnavailable() from exc
