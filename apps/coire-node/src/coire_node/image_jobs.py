"""Private durable journal for one fenced image attempt per job on a Studio."""

from __future__ import annotations

import os
import re
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from coire_core.models.files import ULID_PATTERN
from coire_core.models.image_worker import NodeImageJob, NodeImageStartRequest
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
            if (
                status.job_id != old.job_id
                or status.attempt != old.attempt
                or status.fence != old.fence
                or status.node != old.node
                or status.instance_id != old.instance_id
            ):
                raise ImageJournalConflict()
            if status == old:
                return old
            if old.state in _TERMINAL or status.state not in _NEXT.get(old.state, frozenset()):
                raise ImageJournalConflict()
            if status.updated_at <= old.updated_at:
                raise ImageJournalConflict()
            _write(path, _Record(request=record.request, status=status))
            return status
