"""Receipt-aware deletion of node-owned generated PNG scratch."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coire_core.image_png import ImageRecipeParseError, parse_recipe_png
from coire_core.models.image_worker import (
    ImageTransferReceipt,
    NodeImageCleanupReceipt,
    NodeImageCleanupRequest,
    NodeImageJob,
)
from coire_core.models.images import canonical_recipe_bytes
from coire_node.image_jobs import ImageJobJournal, ImageJournalConflict, ImageJournalUnavailable
from coire_node.metrics import ImageNodeOutcome, ImageNodeStage, image_node_span, record_image_stage


class ImageCleanupUnavailable(RuntimeError):
    """Scratch or receipt evidence is uncertain; keep the journal and bytes."""


def discard_cancelled_image_scratch(
    journal: ImageJobJournal, state_dir: str | Path, status: NodeImageJob
) -> None:
    """Delete only this stopped attempt's private PNGs before confirming cancellation."""
    original = journal.request(status.job_id)
    if (
        original is None
        or original.attempt != status.attempt
        or original.fence != status.fence
        or original.node != status.node
        or original.instance_id != status.instance_id
    ):
        raise ImageJournalConflict()
    root = Path(state_dir) / "image-scratch"
    root_fd = -1
    attempt_fd = -1
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
            raise ImageCleanupUnavailable()
        attempt_key = f"{status.job_id}-{status.attempt}-{status.fence}"
        try:
            attempt_fd = os.open(
                attempt_key,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return
        attempt_info = os.fstat(attempt_fd)
        if attempt_info.st_uid != os.getuid() or attempt_info.st_mode & 0o077:
            raise ImageCleanupUnavailable()
        names = os.listdir(attempt_fd)
        allowed = {f"{index}.png" for index in range(original.resolved.spec.n)}
        if any(name not in allowed for name in names):
            raise ImageCleanupUnavailable()
        for name in names:
            info = os.stat(name, dir_fd=attempt_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ImageCleanupUnavailable()
        for name in names:
            os.unlink(name, dir_fd=attempt_fd)
        os.fsync(attempt_fd)
        os.rmdir(attempt_key, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise ImageCleanupUnavailable() from exc
    finally:
        if attempt_fd >= 0:
            os.close(attempt_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _private_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ImageCleanupUnavailable() from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ImageCleanupUnavailable()


def _verify_output(
    path: Path, receipt: ImageTransferReceipt, status: NodeImageJob, journal: ImageJobJournal
) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ImageCleanupUnavailable() from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ImageCleanupUnavailable()
    try:
        recipe = parse_recipe_png(
            path, expected_size=receipt.byte_count, expected_sha256=receipt.sha256
        )
    except ImageRecipeParseError:
        raise ImageCleanupUnavailable() from None
    original = journal.request(status.job_id)
    if (
        original is None
        or recipe.resolved != original.resolved
        or recipe.output_index != receipt.index
        or hashlib.sha256(canonical_recipe_bytes(recipe)).hexdigest() != receipt.recipe_sha256
    ):
        raise ImageCleanupUnavailable()


def _later(status: NodeImageJob) -> datetime:
    return max(datetime.now(UTC), status.updated_at + timedelta(microseconds=1))


def cleanup_image_outputs(
    journal: ImageJobJournal, state_dir: str | Path, request: NodeImageCleanupRequest
) -> NodeImageCleanupReceipt:
    """Only matching complete core receipts permit unlink, with crash-safe replay."""
    with image_node_span(ImageNodeStage.CLEANUP, job_id=request.job_id):
        try:
            result = _cleanup(journal, Path(state_dir), request)
        except (ImageCleanupUnavailable, ImageJournalConflict, ImageJournalUnavailable):
            record_image_stage(
                ImageNodeStage.CLEANUP, ImageNodeOutcome.FAILED, job_id=request.job_id
            )
            raise
        record_image_stage(
            ImageNodeStage.CLEANUP, ImageNodeOutcome.SUCCEEDED, job_id=request.job_id
        )
        return result


def _cleanup(
    journal: ImageJobJournal, state_dir: Path, request: NodeImageCleanupRequest
) -> NodeImageCleanupReceipt:
    current = journal.get(request.job_id)
    original = journal.request(request.job_id)
    if current is None or original is None:
        raise ImageJournalConflict()
    if (
        current.node != request.node
        or current.attempt != request.attempt
        or current.fence != request.fence
        or request.node != journal.node
    ):
        raise ImageJournalConflict()
    receipts = tuple(sorted(request.receipts, key=lambda item: item.index))
    if tuple(item.index for item in receipts) != tuple(range(original.resolved.spec.n)):
        raise ImageJournalConflict()
    if current.state == "succeeded":
        if current.receipts != receipts or not current.scratch_cleaned:
            raise ImageJournalConflict()
        return NodeImageCleanupReceipt(
            job_id=request.job_id,
            attempt=request.attempt,
            fence=request.fence,
            cleaned_at=current.updated_at,
        )
    if current.state != "transferring":
        raise ImageJournalConflict()
    if current.receipts and current.receipts != receipts:
        raise ImageJournalConflict()

    root = state_dir / "image-scratch"
    attempt_dir = root / f"{request.job_id}-{request.attempt}-{request.fence}"
    _private_dir(root)
    if attempt_dir.is_symlink():
        raise ImageCleanupUnavailable()
    if attempt_dir.exists():
        _private_dir(attempt_dir)
    elif not current.receipts:
        raise ImageCleanupUnavailable()

    if not current.receipts:
        for receipt in receipts:
            _verify_output(attempt_dir / f"{receipt.index}.png", receipt, current, journal)
        current = journal.advance(
            current.model_copy(update={"receipts": receipts, "updated_at": _later(current)})
        )

    try:
        if attempt_dir.exists():
            for receipt in receipts:
                path = attempt_dir / f"{receipt.index}.png"
                if path.exists():
                    _verify_output(path, receipt, current, journal)
                    path.unlink()
            attempt_dir.rmdir()
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ImageCleanupUnavailable):
        raise ImageCleanupUnavailable() from None

    succeeded = journal.advance(
        current.model_copy(
            update={"state": "succeeded", "scratch_cleaned": True, "updated_at": _later(current)}
        )
    )
    return NodeImageCleanupReceipt(
        job_id=request.job_id,
        attempt=request.attempt,
        fence=request.fence,
        cleaned_at=succeeded.updated_at,
    )
