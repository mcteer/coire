"""Shared training code identity, separate from each rank's sealed native graph."""

import hashlib
import json
import sys
from importlib.metadata import distribution
from pathlib import Path

from coire_core.errors import TrainingValidationError

TRAINING_DISTRIBUTIONS = (
    "coire-core",
    "coire-node",
    "mlx",
    "mlx-lm",
    "transformers",
    "tokenizers",
)


def training_code_sha256() -> str:
    """Hash actual installed shared code/resources; never trust asserted RECORD hashes.

    Installed paths, generated launcher wrappers and distribution bookkeeping can
    differ between ranks. They remain covered by the independently verified full
    native graph included in each node's measured hardware/runtime identity.
    """
    root = Path(sys.prefix).resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(
        json.dumps({"identity": "coire-training-code-v1", "python": sys.version_info[:3]}).encode()
    )
    for name in TRAINING_DISTRIBUTIONS:
        installed = distribution(name)
        files = installed.files
        if not files:
            raise TrainingValidationError("Installed training distribution has no file inventory")
        count = 0
        for relative in sorted(files, key=str):
            if (
                ".." in relative.parts
                or "__pycache__" in relative.parts
                or any(part.endswith((".dist-info", ".egg-info")) for part in relative.parts)
            ):
                continue
            path = Path(str(installed.locate_file(relative))).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise TrainingValidationError("Installed training code escapes its environment")
            content = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    content.update(chunk)
            digest.update(
                json.dumps(
                    {"distribution": name, "path": str(relative), "sha256": content.hexdigest()},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            count += 1
        if not count:
            raise TrainingValidationError("Installed training distribution has no code files")
    return digest.hexdigest()
