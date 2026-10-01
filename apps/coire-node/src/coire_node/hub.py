"""Talking to Hugging Face.

This module exists only on a node agent, because the Hugging Face credential exists only there
(spec FR-005). The control plane asks a node to inspect or pull; it never holds the token and
never reaches the Hub itself.

Two decisions live here. **Is this repository MLX-format?** — feature 001 handles only
repositories that need no conversion, and anything else is refused at add time with a pointer
to feature 002. **What shape is the model?** — the sizing keys the memory estimate needs,
which multimodal repositories nest under `text_config` (research R2, R12).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi, hf_hub_download, snapshot_download
from huggingface_hub.errors import (
    EntryNotFoundError,
    GatedRepoError,
    HfHubHTTPError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

from coire_core.models.jobs import JobErrorKind, Quantization, RepoFile, RepoInspection
from coire_core.models.registry import AUXILIARY_IMAGE_KINDS, ModelKind

logger = logging.getLogger(__name__)

WEIGHT_SUFFIXES = (".safetensors",)
GGUF_SUFFIX = ".gguf"
CONFIG_FILE = "config.json"
TOKENIZER_CONFIG_FILE = "tokenizer_config.json"

_IMAGE_SAFE_SUFFIXES = frozenset(
    {".safetensors", ".json", ".txt", ".model", ".tiktoken", ".md", ".yaml", ".yml"}
)
_IMAGE_SAFE_NAMES = frozenset({".gitattributes", "LICENSE", "LICENSE.txt"})
_IMAGE_CLASSIFIER_REPO = "Falconsai/nsfw_image_detection"
_IMAGE_CLASSIFIER_REVISION = "96cb0d0342c7afb80cab76ecc58b265fa44da256"
_IMAGE_CLASSIFIER_SHA256 = "97b2ce64ec146884b37f98ee7944ca4891aa72f6827dc0cb10684a1cbecd5830"
_IMAGE_CLASSIFIER_BYTES = 343_223_968

# Sizing keys, looked up in `text_config` first for multimodal repositories.
_SHAPE_KEYS = (
    "num_hidden_layers",
    "num_key_value_heads",
    "num_attention_heads",
    "head_dim",
    "hidden_size",
    "max_position_embeddings",
)


class HubError(RuntimeError):
    """A Hugging Face operation failed, classified so the caller can say why."""

    def __init__(self, kind: JobErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def classify(exc: Exception) -> HubError:
    """Map a Hub exception to a kind.

    `GatedRepoError` is checked first and deliberately: it *subclasses*
    `RepositoryNotFoundError` for backward compatibility, so the obvious ordering would report
    every gated repository as missing and the operator would go looking for a typo instead of
    accepting a licence (spec edge case 5).
    """
    if isinstance(exc, GatedRepoError):
        return HubError(
            JobErrorKind.GATED,
            "the repository is gated: accept its licence on huggingface.co with the account "
            "whose token this node holds, then retry",
        )
    if isinstance(exc, RepositoryNotFoundError):
        return HubError(JobErrorKind.NOT_FOUND, "no such repository on Hugging Face")
    if isinstance(exc, RevisionNotFoundError):
        return HubError(JobErrorKind.NOT_FOUND, "no such revision in that repository")
    if isinstance(exc, EntryNotFoundError):
        return HubError(JobErrorKind.NOT_FOUND, f"file missing from the repository: {exc}")
    if isinstance(exc, HfHubHTTPError):
        return HubError(JobErrorKind.NETWORK, f"Hugging Face returned an error: {exc}")
    return HubError(JobErrorKind.NETWORK, f"could not reach Hugging Face: {exc}")


def _shape_from(config: dict[str, Any]) -> tuple[dict[str, int | None], bool]:
    """Sizing keys, preferring `text_config` when the repository is multimodal.

    Qwen3.8-27B is `Qwen3_5ForConditionalGeneration` and puts the language model's shape under
    `text_config`; mlx-lm's own loader does the same. Reading the top level there would give
    the vision tower's dimensions or nothing at all, and the memory estimate would be wrong in
    a way nobody would notice until a load failed.
    """
    nested = config.get("text_config")
    source = nested if isinstance(nested, dict) else config
    shape = {key: source.get(key) for key in _SHAPE_KEYS}
    if all(value is None for value in shape.values()) and source is not config:
        shape = {key: config.get(key) for key in _SHAPE_KEYS}
        return shape, False
    return shape, isinstance(nested, dict)


def _is_mlx(config: dict[str, Any], tags: list[str]) -> bool:
    """Whether mlx-lm can load this repository as it stands.

    Two signals, either sufficient (research R2):
      * the Hub `mlx` tag, which every mlx-community conversion carries and which is the only
        signal an unquantised MLX repository (`-bf16`) has; and
      * a top-level `quantization` block in config.json, which is exactly what mlx-lm's
        `load_model` keys on.

    `library_name` is *not* usable: mlx-community repositories report `transformers`. The
    `mlx-community/` name prefix is a heuristic, not a guarantee, and is not used either.
    """
    if "mlx" in tags:
        return True
    quant = config.get("quantization")
    return isinstance(quant, dict) and "bits" in quant


def inspect(
    repo_id: str,
    *,
    revision: str = "main",
    token: str | None = None,
    cache_dir: str | None = None,
) -> RepoInspection:
    """Read a repository's metadata without moving any weights (spec FR-010).

    Downloads only `config.json` and `tokenizer_config.json`, which are kilobytes.
    """
    api = HfApi(token=token or None)
    try:
        info = api.model_info(repo_id, revision=revision, files_metadata=True)
    except Exception as exc:
        raise classify(exc) from exc

    files: list[RepoFile] = []
    total = 0
    weights = 0
    has_gguf = False
    for sibling in info.siblings or []:
        name = sibling.rfilename
        size = int(sibling.size or 0)
        lfs = getattr(sibling, "lfs", None)
        upstream = lfs.sha256 if lfs is not None else None
        files.append(RepoFile(path=name, bytes=size, upstream_sha256=upstream))
        total += size
        if name.endswith(WEIGHT_SUFFIXES):
            weights += size
        if name.endswith(GGUF_SUFFIX):
            has_gguf = True

    resolved = info.sha or revision
    config: dict[str, Any] = {}
    tokenizer_config: dict[str, Any] = {}
    for filename, target in ((CONFIG_FILE, config), (TOKENIZER_CONFIG_FILE, tokenizer_config)):
        try:
            path = hf_hub_download(
                repo_id,
                filename,
                revision=resolved,
                token=token or None,
                cache_dir=cache_dir,
            )
            target.update(json.loads(Path(path).read_text()))
        except EntryNotFoundError:
            logger.info("%s has no %s", repo_id, filename)
        except Exception as exc:
            raise classify(exc) from exc

    shape, from_text_config = _shape_from(config)
    quant_raw = config.get("quantization")
    quantization = None
    if isinstance(quant_raw, dict):
        quantization = Quantization(
            bits=quant_raw.get("bits"),
            group_size=quant_raw.get("group_size"),
            mode=quant_raw.get("mode"),
        )

    architectures = config.get("architectures") or []
    card_data = getattr(info, "card_data", None)
    card_license = (
        card_data.get("license")
        if isinstance(card_data, dict)
        else getattr(card_data, "license", None)
    )
    license_id = card_license if isinstance(card_license, str) and card_license else None
    if license_id is None:
        license_id = next(
            (tag.removeprefix("license:") for tag in info.tags or [] if tag.startswith("license:")),
            None,
        )
    return RepoInspection(
        repo_id=repo_id,
        revision=resolved,
        files=files,
        total_bytes=total,
        # A repository with no *.safetensors (GGUF-only) has no weight bytes; fall back to the
        # total so the rejection message can still quote a size.
        weight_bytes=weights or total,
        is_mlx_format=_is_mlx(config, list(info.tags or [])),
        has_gguf_only=has_gguf and weights == 0,
        gated=False,  # a gated repo raises above; reaching here means it is readable
        license_id=license_id,
        architecture=(architectures[0] if architectures else config.get("model_type")),
        quantization=quantization,
        torch_dtype=config.get("torch_dtype") or config.get("dtype"),
        max_position_embeddings=shape.get("max_position_embeddings"),
        chat_template_present=bool(tokenizer_config.get("chat_template")),
        num_hidden_layers=shape.get("num_hidden_layers"),
        num_key_value_heads=shape.get("num_key_value_heads"),
        head_dim=shape.get("head_dim"),
        hidden_size=shape.get("hidden_size"),
        num_attention_heads=shape.get("num_attention_heads"),
        sizing_from_text_config=from_text_config,
    )


def snapshot(
    repo_id: str,
    *,
    revision: str,
    local_dir: str | Path,
    token: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Download a repository into the store.

    `local_dir` writes plain files rather than a cache of symlinks, because an engine is
    pointed at the directory and must be able to read it without resolving links into a cache
    whose layout is not ours.

    Resumption is per file: huggingface_hub 1.29 writes each file to a unique `.incomplete`
    and deletes it on failure (issue #4196), so an interrupted download re-fetches only the
    files that were incomplete. Hub repositories shard weights at 5 GB, which bounds the loss.
    """
    try:
        return snapshot_download(
            repo_id,
            revision=revision,
            local_dir=str(local_dir),
            token=token or None,
            max_workers=4,
        )
    except Exception as exc:
        raise classify(exc) from exc


def image_asset_files(repo: RepoInspection, kind: ModelKind) -> tuple[str, ...]:
    """Select exact inert files for a pinned image acquisition, before any weight transfer."""
    if kind is not ModelKind.IMAGE_MODEL and kind not in AUXILIARY_IMAGE_KINDS:
        raise ValueError("image asset kind required")
    if not re.fullmatch(r"[0-9a-f]{40}", repo.revision) or repo.revision == "0" * 40:
        raise ValueError("image asset requires a resolved commit")
    if not repo.files or len(repo.files) > 4096:
        raise ValueError("image asset file count is invalid")

    seen: set[str] = set()
    selected: dict[str, RepoFile] = {}
    for item in repo.files:
        path = item.path
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
            or any(char in path for char in "*?[]")
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or path in seen
        ):
            raise ValueError("image asset has an unsafe or duplicate path")
        seen.add(path)
        if Path(path).suffix not in _IMAGE_SAFE_SUFFIXES and path not in _IMAGE_SAFE_NAMES:
            continue
        if path.endswith(".safetensors") and (
            item.bytes <= 0
            or item.upstream_sha256 is None
            or not re.fullmatch(r"[0-9a-f]{64}", item.upstream_sha256)
        ):
            raise ValueError("image asset weight lacks an upstream digest")
        selected[path] = item

    weights = {path for path in selected if path.endswith(".safetensors")}
    if not weights:
        raise ValueError("image asset has no safetensors weights")
    if (
        kind in {ModelKind.IMAGE_MODEL, ModelKind.CONTROL_MODEL, ModelKind.UPSCALE_MODEL}
        and "config.json" not in selected
    ):
        raise ValueError("image asset has no local config")
    if kind is ModelKind.IMAGE_CLASSIFIER:
        weight = selected.get("model.safetensors")
        if (
            repo.repo_id != _IMAGE_CLASSIFIER_REPO
            or repo.revision != _IMAGE_CLASSIFIER_REVISION
            or "config.json" not in selected
            or "preprocessor_config.json" not in selected
            or weights != {"model.safetensors"}
            or weight is None
            or weight.bytes != _IMAGE_CLASSIFIER_BYTES
            or weight.upstream_sha256 != _IMAGE_CLASSIFIER_SHA256
        ):
            raise ValueError("classifier asset differs from pinned manifest")
    return tuple(sorted(selected))


def snapshot_image_asset(
    repo: RepoInspection,
    kind: ModelKind,
    *,
    local_dir: str | Path,
    token: str | None = None,
) -> str:
    """Pull only selected image files; the admin workflow owns licence and publication checks."""
    allowed = image_asset_files(repo, kind)
    try:
        return snapshot_download(
            repo.repo_id,
            revision=repo.revision,
            local_dir=str(local_dir),
            token=token or None,
            allow_patterns=list(allowed),
            max_workers=4,
        )
    except Exception as exc:
        raise classify(exc) from exc
