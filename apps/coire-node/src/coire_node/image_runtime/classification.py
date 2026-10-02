"""Pinned, local-only Studio CPU tagging with a killed-on-failure subprocess."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import stat
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import psutil

from coire_core.models.images import ImageClassificationResult, ImageContentTag
from coire_core.models.registry import slug_for
from coire_node.image_runtime.preflight import ImageCopyUnavailable, _safe_local_tree
from coire_node.metrics import (
    ImageNodeOutcome,
    ImageNodeStage,
    image_node_span,
    record_image_stage,
)
from coire_node.store import Store, StoreError

CLASSIFIER_REPO_ID = "Falconsai/nsfw_image_detection"
CLASSIFIER_REVISION = "96cb0d0342c7afb80cab76ecc58b265fa44da256"
CLASSIFIER_WEIGHT_SHA256 = "97b2ce64ec146884b37f98ee7944ca4891aa72f6827dc0cb10684a1cbecd5830"
CLASSIFIER_WEIGHT_BYTES = 343_223_968
CLASSIFIER_DEADLINE_S = 10.0
CLASSIFIER_THRESHOLD = Decimal("0.5")

SafeError = Literal[
    "classifier_failed", "classifier_timeout", "classifier_memory", "classifier_invalid_result"
]


def verified_classifier_copy(store: Store) -> Path | None:
    """Find an exact offline classifier copy without acquiring or trusting a path from a job."""
    slug = slug_for(CLASSIFIER_REPO_ID)
    try:
        if not stat.S_ISDIR((store.root / slug).lstat().st_mode):
            return None
        if not stat.S_ISREG(store.manifest_path(slug).lstat().st_mode):
            return None
        manifest = store.read_manifest(slug)
        if (
            manifest is None
            or manifest.slug != slug
            or manifest.repo_id != CLASSIFIER_REPO_ID
            or manifest.revision != CLASSIFIER_REVISION
        ):
            return None
        weights = [item for item in manifest.files if item.path == "model.safetensors"]
        if (
            len(weights) != 1
            or weights[0].bytes != CLASSIFIER_WEIGHT_BYTES
            or weights[0].sha256 != CLASSIFIER_WEIGHT_SHA256
            or not {"config.json", "preprocessor_config.json"}.issubset(
                {item.path for item in manifest.files}
            )
            or sum(item.bytes for item in manifest.files) != manifest.total_bytes
        ):
            return None
        root = store.path_for(slug)
        _safe_local_tree(root)
        if store.verify_against(slug, manifest):
            return None
        return root
    except (OSError, StoreError, ImageCopyUnavailable, ValueError):
        return None


def _unknown(code: SafeError) -> ImageClassificationResult:
    return ImageClassificationResult(
        tag=ImageContentTag.UNKNOWN,
        threshold=CLASSIFIER_THRESHOLD,
        classifier_revision=CLASSIFIER_REVISION,
        safe_error=code,
        tagged_at=datetime.now(UTC),
    )


def apply_explicit_policy(
    result: ImageClassificationResult, *, explicit: bool
) -> ImageClassificationResult:
    """Policy can raise the tag but classification never revokes authorization."""
    if not explicit or result.tag is ImageContentTag.EXPLICIT:
        return result
    return result.model_copy(update={"tag": ImageContentTag.EXPLICIT})


def process_rss_bytes(pid: int) -> int:
    try:
        return int(psutil.Process(pid).memory_info().rss)
    except psutil.NoSuchProcess:
        return 0


def _offline_environment() -> dict[str, str]:
    """Pass only runtime essentials; never give the classifier a Hub credential."""
    result = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONNOUSERSITE": "1",
        "CUDA_VISIBLE_DEVICES": "",
        "PYTORCH_ENABLE_MPS_FALLBACK": "0",
        "OMP_NUM_THREADS": "2",
    }
    if value := os.environ.get("TMPDIR"):
        result["TMPDIR"] = value
    return result


async def _kill_and_wait(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        process.kill()
    await process.wait()


async def classify_image(
    model_dir: Path,
    image_path: Path,
    *,
    reservation_bytes: int,
    policy_explicit: bool = False,
    timeout_s: float = CLASSIFIER_DEADLINE_S,
    job_id: str | None = None,
) -> ImageClassificationResult:
    """Supervise one local CPU tagging stage and return an owner-private fallback."""
    result, _ = await classify_image_with_peak(
        model_dir,
        image_path,
        reservation_bytes=reservation_bytes,
        policy_explicit=policy_explicit,
        timeout_s=timeout_s,
        job_id=job_id,
    )
    return result


async def classify_image_with_peak(
    model_dir: Path,
    image_path: Path,
    *,
    reservation_bytes: int,
    policy_explicit: bool = False,
    timeout_s: float = CLASSIFIER_DEADLINE_S,
    job_id: str | None = None,
) -> tuple[ImageClassificationResult, int]:
    """Return sampled child RSS with the bounded classification result."""
    with image_node_span(ImageNodeStage.CLASSIFY, job_id=job_id):
        return await _classify_image_stage(
            model_dir,
            image_path,
            reservation_bytes=reservation_bytes,
            policy_explicit=policy_explicit,
            timeout_s=timeout_s,
            job_id=job_id,
        )


async def _classify_image_stage(
    model_dir: Path,
    image_path: Path,
    *,
    reservation_bytes: int,
    policy_explicit: bool,
    timeout_s: float,
    job_id: str | None,
) -> tuple[ImageClassificationResult, int]:
    started = time.monotonic()
    outcome = ImageNodeOutcome.FAILED
    process: asyncio.subprocess.Process | None = None
    peak_rss_bytes = 0
    try:
        if reservation_bytes <= 0 or not 0 < timeout_s <= CLASSIFIER_DEADLINE_S:
            result = _unknown("classifier_failed")
        else:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "coire_node.image_runtime.classification",
                "--child",
                str(model_dir),
                str(image_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_offline_environment(),
            )
            peak_rss_bytes = process_rss_bytes(process.pid)
            deadline = started + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    peak_rss_bytes = max(peak_rss_bytes, process_rss_bytes(process.pid))
                    await _kill_and_wait(process)
                    result = _unknown("classifier_timeout")
                    break
                try:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=min(remaining, 0.05))
                except TimeoutError:
                    peak_rss_bytes = max(peak_rss_bytes, process_rss_bytes(process.pid))
                    if peak_rss_bytes > reservation_bytes:
                        await _kill_and_wait(process)
                        result = _unknown("classifier_memory")
                        break
                    continue
                if exit_code != 0:
                    result = _unknown("classifier_failed")
                    break
                raw, _ = await process.communicate()
                try:
                    if len(raw) > 4096:
                        raise ValueError("classifier result too large")
                    candidate = ImageClassificationResult.model_validate_json(raw)
                    if (
                        candidate.classifier_revision != CLASSIFIER_REVISION
                        or candidate.threshold != CLASSIFIER_THRESHOLD
                        or candidate.score is None
                        or candidate.tag
                        is not (
                            ImageContentTag.EXPLICIT
                            if candidate.score >= CLASSIFIER_THRESHOLD
                            else ImageContentTag.NORMAL
                        )
                    ):
                        raise ValueError("classifier result conflicts with pinned policy")
                    result = candidate
                except ValueError:
                    result = _unknown("classifier_invalid_result")
                break
    except Exception:
        result = _unknown("classifier_failed")
    finally:
        if process is not None and process.returncode is None:
            await _kill_and_wait(process)
    result = apply_explicit_policy(result, explicit=policy_explicit)
    if result.safe_error is None:
        outcome = ImageNodeOutcome.SUCCEEDED
    record_image_stage(
        ImageNodeStage.CLASSIFY,
        outcome,
        duration_s=time.monotonic() - started,
        job_id=job_id,
    )
    return result, peak_rss_bytes


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classify_local(model_dir: Path, image_path: Path) -> ImageClassificationResult:
    """Run the pinned safetensors classifier directly on CPU in the child process."""
    weight = model_dir / "model.safetensors"
    processor_config = model_dir / "preprocessor_config.json"
    if (
        weight.stat().st_size != CLASSIFIER_WEIGHT_BYTES
        or _digest(weight) != CLASSIFIER_WEIGHT_SHA256
    ):
        raise ValueError("classifier weight differs from pinned manifest")
    processor_sha = _digest(processor_config)

    from PIL import Image

    torch = importlib.import_module("torch")
    transformers = importlib.import_module("transformers")

    processor = transformers.AutoImageProcessor.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False
    )
    model = transformers.AutoModelForImageClassification.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False, use_safetensors=True
    )
    model.to("cpu")
    model.eval()
    labels = {int(index): str(label).lower() for index, label in model.config.id2label.items()}
    explicit_indices = [index for index, label in labels.items() if label in {"nsfw", "explicit"}]
    if len(explicit_indices) != 1:
        raise ValueError("classifier labels differ from pinned policy")
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
    with torch.inference_mode():
        logits = model(**inputs).logits
        score = float(torch.softmax(logits, dim=-1)[0, explicit_indices[0]].item())
    probability = Decimal(str(score))
    return ImageClassificationResult(
        tag=(
            ImageContentTag.EXPLICIT
            if probability >= CLASSIFIER_THRESHOLD
            else ImageContentTag.NORMAL
        ),
        score=probability,
        threshold=CLASSIFIER_THRESHOLD,
        classifier_revision=CLASSIFIER_REVISION,
        processor_sha256=processor_sha,
        tagged_at=datetime.now(UTC),
    )


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "--child":
        return 2
    try:
        result = _classify_local(Path(sys.argv[2]), Path(sys.argv[3]))
    except Exception:
        return 1
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
