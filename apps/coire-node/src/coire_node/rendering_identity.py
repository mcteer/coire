"""Inspect acquired rendering metadata without importing MLX or encoding tokens."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from coire_core.models.engine import EngineRenderingIdentity
from coire_core.models.jobs import ChecksumManifest

_PREFIXES = ("tokenizer", "vocab", "merges", "special_tokens", "added_tokens")
_PACKAGES = ("mlx", "mlx-lm", "transformers", "tokenizers")


def inspect_rendering_identity(
    root: Path,
    manifest: ChecksumManifest,
    *,
    template_override: str | None = None,
    versions: dict[str, str] | None = None,
) -> EngineRenderingIdentity | None:
    """Return no provenance when acquired files cannot prove effective rendering.

    Raw Jinja files take priority over tokenizer_config, matching the pinned
    Transformers loader. Named template dictionaries remain unsupported by the
    paired analysis path, so they cannot qualify a serving snapshot either.
    """
    try:
        listed = {item.path: item for item in manifest.files}
        if len(listed) != len(manifest.files) or "tokenizer_config.json" not in listed:
            return None
        if not {"tokenizer.json", "tokenizer.model"} & listed.keys():
            return None
        for item in manifest.files:
            if not item.path.startswith((*_PREFIXES, "chat_template")):
                continue
            path = root / item.path
            if (
                any(parent.is_symlink() for parent in (path, *path.parents))
                or not path.is_file()
                or item.bytes > 256 * 1024**2
                or path.stat().st_size != item.bytes
            ):
                return None
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item.sha256:
                    return None
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or (
                path.is_file()
                and relative.startswith((*_PREFIXES, "chat_template"))
                and relative not in listed
            ):
                return None
        config_path = root / "tokenizer_config.json"
        if config_path.stat().st_size > 1024**2:
            return None
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if (
            not isinstance(config, dict)
            or config.get("auto_map")
            or config.get("chat_template_type")
        ):
            return None
        template = template_override
        if template is None:
            if any(name.startswith("chat_templates/") for name in listed):
                return None
            if "chat_template.jinja" in listed:
                path = root / "chat_template.jinja"
                if path.stat().st_size > 1024**2:
                    return None
                template = path.read_text(encoding="utf-8")
            else:
                template = config.get("chat_template")
        if not isinstance(template, str) or not template or len(template.encode()) > 1024**2:
            return None
        packages = versions if versions is not None else {name: version(name) for name in _PACKAGES}
        if set(packages) != set(_PACKAGES) or any(not value for value in packages.values()):
            return None
        tokenizer_files = sorted(
            (item.path, item.sha256) for item in manifest.files if item.path.startswith(_PREFIXES)
        )
        return EngineRenderingIdentity(
            tokenizer_sha256=hashlib.sha256(
                json.dumps(tokenizer_files, separators=(",", ":")).encode()
            ).hexdigest(),
            template_sha256=hashlib.sha256(template.encode()).hexdigest(),
            runtime_sha256=hashlib.sha256(
                json.dumps(packages, sort_keys=True).encode()
            ).hexdigest(),
        )
    except (OSError, ValueError, PackageNotFoundError):
        return None
