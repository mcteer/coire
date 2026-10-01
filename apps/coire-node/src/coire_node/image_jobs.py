"""Private durable journal for one fenced image attempt per job on a Studio."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import threading
import uuid
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict

from coire_core.models.files import ULID_PATTERN
from coire_core.models.image_worker import (
    NodeImageInputReceipt,
    NodeImageInputRequest,
    NodeImageJob,
    NodeImageStartRequest,
)
from coire_node.metrics import ImageNodeOutcome, ImageNodeStage, image_node_span, record_image_stage
from coire_node.store import write_atomic

_MAX_RECORD_BYTES = 128 * 1024
_TERMINAL = frozenset({"cancelled", "failed", "succeeded"})
_NEXT: dict[str, frozenset[str]] = {
    "queued": frozenset({"reserving", "cancelling", "cancelled", "failed"}),
    "reserving": frozenset({"running", "cancelling", "cancelled", "failed"}),
    "running": frozenset({"transferring", "cancelling", "cancelled", "failed"}),
    "transferring": frozenset({"succeeded", "cancelling", "cancelled", "failed"}),
    "cancelling": frozenset({"cancelled", "failed"}),
}


class ImageJournalUnavailable(RuntimeError):
    """Private state cannot be trusted; retain reservations and do not rerun."""


class ImageJournalConflict(RuntimeError):
    """A changed attempt or invalid state transition was requested."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    request: NodeImageStartRequest
    status: NodeImageJob


def _private_root(root: Path) -> None:
    try:
        if root.is_symlink():
            raise ImageJournalUnavailable()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ImageJournalUnavailable()
    except OSError:
        raise ImageJournalUnavailable() from None


def _read(path: Path) -> _Record | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise ImageJournalUnavailable() from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > _MAX_RECORD_BYTES
        ):
            raise ImageJournalUnavailable()
        with os.fdopen(fd, "rb", closefd=False) as source:
            payload = source.read(_MAX_RECORD_BYTES + 1)
        if len(payload) > _MAX_RECORD_BYTES:
            raise ImageJournalUnavailable()
        record = _Record.model_validate_json(payload)
        if (
            record.request.job_id != record.status.job_id
            or record.request.attempt != record.status.attempt
            or record.request.fence != record.status.fence
            or record.request.node != record.status.node
            or record.request.instance_id != record.status.instance_id
        ):
            raise ImageJournalUnavailable()
        return record
    except (OSError, ValueError):
        raise ImageJournalUnavailable() from None
    finally:
        os.close(fd)


def _write(path: Path, record: _Record) -> None:
    payload = record.model_dump_json().encode("utf-8")
    if len(payload) > _MAX_RECORD_BYTES:
        raise ImageJournalUnavailable()
    with image_node_span(ImageNodeStage.JOURNAL, job_id=record.request.job_id):
        try:
            write_atomic(path, payload)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            record_image_stage(
                ImageNodeStage.JOURNAL, ImageNodeOutcome.FAILED, job_id=record.request.job_id
            )
            raise ImageJournalUnavailable() from None
        record_image_stage(
            ImageNodeStage.JOURNAL, ImageNodeOutcome.SUCCEEDED, job_id=record.request.job_id
        )


class ImageJobJournal:
    """One durable request per ULID; status survives node-agent restarts."""

    def __init__(self, state_dir: str | Path, node: str) -> None:
        self.root = Path(state_dir) / "image-jobs"
        self.node = node
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        if re.fullmatch(ULID_PATTERN, job_id) is None:
            raise ImageJournalConflict()
        return self.root / f"{job_id}.json"

    def _record(self, job_id: str) -> _Record | None:
        record = _read(self._path(job_id))
        if record is not None and record.request.node != self.node:
            raise ImageJournalUnavailable()
        return record

    def begin(self, request: NodeImageStartRequest) -> NodeImageJob:
        if request.node != self.node:
            raise ImageJournalConflict()
        with self._lock:
            _private_root(self.root)
            path = self._path(request.job_id)
            existing = self._record(request.job_id)
            if existing is not None:
                if existing.request != request:
                    raise ImageJournalConflict()
                return existing.status
            if request.deadline_at <= datetime.now(UTC):
                raise ImageJournalConflict()
            status = NodeImageJob(
                job_id=request.job_id,
                attempt=request.attempt,
                fence=request.fence,
                node=request.node,
                instance_id=request.instance_id,
                state="queued",
                updated_at=datetime.now(UTC),
            )
            _write(path, _Record(request=request, status=status))
            return status

    def get(self, job_id: str) -> NodeImageJob | None:
        with self._lock:
            _private_root(self.root)
            record = self._record(job_id)
            return record.status if record is not None else None

    def request(self, job_id: str) -> NodeImageStartRequest | None:
        with self._lock:
            _private_root(self.root)
            record = self._record(job_id)
            return record.request if record is not None else None

    def advance(self, status: NodeImageJob) -> NodeImageJob:
        try:
            status = NodeImageJob.model_validate(status.model_dump())
        except ValueError:
            raise ImageJournalConflict() from None
        with self._lock:
            _private_root(self.root)
            path = self._path(status.job_id)
            record = self._record(status.job_id)
            if record is None:
                raise ImageJournalConflict()
            old = record.status
            initial_process_binding = (
                old.state == "queued"
                and status.state == "reserving"
                and old.pid is None
                and old.process_create_time is None
                and status.pid is not None
                and status.process_create_time is not None
            )
            if (
                status.job_id != old.job_id
                or status.attempt != old.attempt
                or status.fence != old.fence
                or status.node != old.node
                or status.instance_id != old.instance_id
                or (
                    not initial_process_binding
                    and (
                        status.pid != old.pid
                        or status.process_create_time != old.process_create_time
                    )
                )
            ):
                raise ImageJournalConflict()
            if status == old:
                return old
            cancelled_cleanup_repair = (
                old.state == status.state == "cancelled"
                and not old.scratch_cleaned
                and status.scratch_cleaned
                and status.model_dump(exclude={"scratch_cleaned", "updated_at"})
                == old.model_dump(exclude={"scratch_cleaned", "updated_at"})
            )
            if (old.state in _TERMINAL and not cancelled_cleanup_repair) or (
                status.state != old.state and status.state not in _NEXT.get(old.state, frozenset())
            ):
                raise ImageJournalConflict()
            if (
                old.progress_step is not None
                and (status.progress_step is None or status.progress_step < old.progress_step)
            ) or (old.progress_total is not None and status.progress_total != old.progress_total):
                raise ImageJournalConflict()
            if status.receipts[: len(old.receipts)] != old.receipts:
                raise ImageJournalConflict()
            if old.outputs and status.outputs != old.outputs:
                raise ImageJournalConflict()
            if status.outputs != old.outputs and status.state != "transferring":
                raise ImageJournalConflict()
            if status.receipts != old.receipts and old.state != "transferring":
                raise ImageJournalConflict()
            if old.scratch_cleaned and not status.scratch_cleaned:
                raise ImageJournalConflict()
            if status.updated_at <= old.updated_at:
                raise ImageJournalConflict()
            _write(path, _Record(request=record.request, status=status))
            return status


def _write_input_block(fd: int, block: bytes) -> None:
    view = memoryview(block)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ImageJournalUnavailable()
        view = view[written:]


async def stage_node_image_input(
    journal: ImageJobJournal,
    state_dir: str | Path,
    command: NodeImageInputRequest,
    chunks: AsyncIterable[bytes],
) -> NodeImageInputReceipt:
    """Stage one normalized PNG under an exact queued job/fence binding."""
    if command.node != journal.node:
        raise ImageJournalConflict()
    original = journal.request(command.job_id)
    current = journal.get(command.job_id)
    if (
        original is None
        or current is None
        or current.state != "queued"
        or original.deadline_at <= datetime.now(UTC)
        or (original.attempt, original.fence) != (command.attempt, command.fence)
    ):
        raise ImageJournalConflict()
    expected = next((item for item in original.inputs if item.input_id == command.input_id), None)
    if (
        expected is None
        or expected.purpose != command.purpose
        or expected.sha256 != command.sha256
        or expected.byte_count != command.byte_count
    ):
        raise ImageJournalConflict()
    root = Path(state_dir) / "image-input-scratch"
    _private_root(root)
    attempt = root / f"{command.job_id}-{command.attempt}-{command.fence}"
    _private_root(attempt)
    target = attempt / str(command.input_id)
    temporary = attempt / f".{command.input_id}.{uuid.uuid4()}.uploading"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ImageJournalUnavailable() from exc
    digest = hashlib.sha256()
    size = 0
    try:
        async for block in chunks:
            if original.deadline_at <= datetime.now(UTC):
                raise ImageJournalConflict()
            size += len(block)
            if size > command.byte_count:
                raise ImageJournalConflict()
            digest.update(block)
            await asyncio.to_thread(_write_input_block, fd, block)
        if (
            original.deadline_at <= datetime.now(UTC)
            or size != command.byte_count
            or digest.hexdigest() != command.sha256
        ):
            raise ImageJournalConflict()
        await asyncio.to_thread(os.fsync, fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        latest = journal.get(command.job_id)
        if (
            latest is None
            or latest.state != "queued"
            or (latest.attempt, latest.fence) != (command.attempt, command.fence)
            or original.deadline_at <= datetime.now(UTC)
        ):
            raise ImageJournalConflict()
        await asyncio.to_thread(
            _check_staged_png, temporary, expected.width, expected.height, command.purpose
        )
        if original.deadline_at <= datetime.now(UTC):
            raise ImageJournalConflict()
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if not _same_staged_file(target, temporary, command.byte_count, command.sha256):
                raise ImageJournalConflict() from None
        directory = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return NodeImageInputReceipt(**command.model_dump(), staged_at=datetime.now(UTC))
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ImageJournalUnavailable() from exc
    finally:
        temporary.unlink(missing_ok=True)


def _check_staged_png(path: Path, width: int, height: int, purpose: str) -> None:
    try:
        with Image.open(path) as image:
            if (
                image.format != "PNG"
                or image.size != (width, height)
                or image.mode != ("L" if purpose == "mask" else "RGB")
                or getattr(image, "is_animated", False)
            ):
                raise ImageJournalConflict()
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ImageJournalConflict() from exc


def _same_staged_file(target: Path, temporary: Path, size: int, digest: str) -> bool:
    existing_fd = -1
    try:
        existing_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(existing_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size != size
        ):
            return False
        with (
            os.fdopen(existing_fd, "rb", closefd=False) as existing,
            temporary.open("rb") as staged,
        ):
            return (
                hashlib.file_digest(existing, "sha256").hexdigest() == digest
                and existing.seek(0) == 0
                and existing.read() == staged.read()
            )
    except OSError:
        return False
    finally:
        if existing_fd >= 0:
            os.close(existing_fd)


def discard_node_image_inputs(
    journal: ImageJobJournal, state_dir: str | Path, status: NodeImageJob
) -> None:
    """Remove only this stopped attempt's staged inputs; absence is replay-safe."""
    original = journal.request(status.job_id)
    if original is None or (original.attempt, original.fence, original.node) != (
        status.attempt,
        status.fence,
        status.node,
    ):
        raise ImageJournalConflict()
    root = Path(state_dir) / "image-input-scratch"
    root_fd = -1
    attempt_fd = -1
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
            raise ImageJournalUnavailable()
        attempt_key = f"{status.job_id}-{status.attempt}-{status.fence}"
        try:
            attempt_fd = os.open(
                attempt_key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
            )
        except FileNotFoundError:
            return
        attempt_info = os.fstat(attempt_fd)
        if attempt_info.st_uid != os.getuid() or attempt_info.st_mode & 0o077:
            raise ImageJournalUnavailable()
        allowed = {str(item.input_id) for item in original.inputs}
        names = os.listdir(attempt_fd)
        for name in names:
            temporary_id = name.split(".", 2)[1] if name.startswith(".") else None
            if name not in allowed and temporary_id not in allowed:
                raise ImageJournalUnavailable()
            if temporary_id is not None and not name.endswith(".uploading"):
                raise ImageJournalUnavailable()
            info = os.stat(name, dir_fd=attempt_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ImageJournalUnavailable()
        for name in names:
            os.unlink(name, dir_fd=attempt_fd)
        os.fsync(attempt_fd)
        os.rmdir(attempt_key, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise ImageJournalUnavailable() from exc
    finally:
        if attempt_fd >= 0:
            os.close(attempt_fd)
        if root_fd >= 0:
            os.close(root_fd)
