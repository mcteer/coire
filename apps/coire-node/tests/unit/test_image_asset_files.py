"""Image acquisition selects only safe, pinned files before a Hub snapshot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coire_core.models.jobs import RepoFile, RepoInspection
from coire_core.models.registry import ModelKind
from coire_node import hub

REVISION = "96cb0d0342c7afb80cab76ecc58b265fa44da256"
WEIGHT_SHA = "97b2ce64ec146884b37f98ee7944ca4891aa72f6827dc0cb10684a1cbecd5830"


def _repo(
    *,
    revision: str = REVISION,
    files: list[RepoFile] | None = None,
) -> RepoInspection:
    return RepoInspection(
        repo_id="Falconsai/nsfw_image_detection",
        revision=revision,
        files=files
        if files is not None
        else [
            RepoFile(path=".gitattributes", bytes=1519),
            RepoFile(path="README.md", bytes=10640),
            RepoFile(path="config.json", bytes=724),
            RepoFile(path="model.safetensors", bytes=343223968, upstream_sha256=WEIGHT_SHA),
            RepoFile(path="preprocessor_config.json", bytes=325),
            RepoFile(path="optimizer.pt", bytes=686518917),
            RepoFile(path="pytorch_model.bin", bytes=343268717),
        ],
        total_bytes=1_100_000_000,
        weight_bytes=343223968,
        is_mlx_format=False,
    )


def test_classifier_selects_only_pinned_safe_files() -> None:
    selected = hub.image_asset_files(_repo(), ModelKind.IMAGE_CLASSIFIER)
    assert selected == (
        ".gitattributes",
        "README.md",
        "config.json",
        "model.safetensors",
        "preprocessor_config.json",
    )
    assert all(not item.endswith((".pt", ".bin")) for item in selected)


@pytest.mark.parametrize(
    ("repo", "kind"),
    [
        (_repo(revision="main"), ModelKind.IMAGE_CLASSIFIER),
        (_repo(revision="0" * 40), ModelKind.IMAGE_CLASSIFIER),
        (
            _repo(
                files=[RepoFile(path="../model.safetensors", bytes=1, upstream_sha256=WEIGHT_SHA)]
            ),
            ModelKind.IMAGE_LORA,
        ),
        (
            _repo(files=[RepoFile(path="a\\b.safetensors", bytes=1, upstream_sha256=WEIGHT_SHA)]),
            ModelKind.IMAGE_LORA,
        ),
        (
            _repo(
                files=[
                    RepoFile(path="adapter.safetensors", bytes=1, upstream_sha256=WEIGHT_SHA),
                    RepoFile(path="adapter.safetensors", bytes=1, upstream_sha256=WEIGHT_SHA),
                ]
            ),
            ModelKind.IMAGE_LORA,
        ),
        (_repo(files=[RepoFile(path="config.json", bytes=10)]), ModelKind.IMAGE_MODEL),
        (
            _repo(files=[RepoFile(path="model.safetensors", bytes=1)]),
            ModelKind.IMAGE_LORA,
        ),
        (
            _repo(
                files=[
                    RepoFile(path="model.safetensors", bytes=343223968, upstream_sha256="0" * 64),
                    RepoFile(path="config.json", bytes=724),
                    RepoFile(path="preprocessor_config.json", bytes=325),
                ]
            ),
            ModelKind.IMAGE_CLASSIFIER,
        ),
    ],
)
def test_image_manifest_refuses_unpinned_incomplete_or_unsafe(
    repo: RepoInspection, kind: ModelKind
) -> None:
    with pytest.raises(ValueError):
        hub.image_asset_files(repo, kind)


def test_filtered_snapshot_uses_exact_selected_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download(*args: object, **kwargs: object) -> str:
        calls.append(kwargs)
        return str(tmp_path)

    monkeypatch.setattr(hub, "snapshot_download", fake_download)
    result = hub.snapshot_image_asset(
        _repo(), ModelKind.IMAGE_CLASSIFIER, local_dir=tmp_path, token="internal-token"
    )
    assert result == str(tmp_path)
    assert len(calls) == 1
    assert calls[0]["revision"] == REVISION
    assert calls[0]["allow_patterns"] == list(
        hub.image_asset_files(_repo(), ModelKind.IMAGE_CLASSIFIER)
    )
    assert calls[0]["local_dir"] == str(tmp_path)
    with pytest.raises(ValueError):
        hub.snapshot_image_asset(
            _repo(revision="main"), ModelKind.IMAGE_CLASSIFIER, local_dir=tmp_path
        )
    assert len(calls) == 1
