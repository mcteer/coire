"""Supervising acquisition jobs.

The agent owns job *state*; the worker subprocess does the work. That split is what makes the
control plane's re-issue safe: `start` is idempotent on the caller's job id, so a restarted
control plane that repeats the current stage gets the existing job back rather than a second
download (ADR-0005).

The state files are a cache, not truth. If the registry and a node disagree, the registry's
reconcile wins (Principle II) — these files exist so the agent can find its own work again
after a restart, not so anyone can ask them what is true.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from coire_core.models.jobs import JobErrorKind, JobKind, JobStage, JobStatus
from coire_core.settings import Settings
from coire_node.store import Store, write_atomic

logger = logging.getLogger(__name__)

JOB_FILE_SUFFIX = ".json"


class JobConflict(RuntimeError):
    """Another job is already writing this slug."""


class InsufficientSpace(RuntimeError):
    """The store cannot hold what the job would write."""

    def __init__(self, required: int, free: int) -> None:
        super().__init__(f"needs {required} bytes, {free} free")
        self.required = required
        self.free = free


class JobSupervisor:
    """Starts, tracks, resumes and cancels acquisition workers."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._dir = Path(settings.node_state_dir) / "jobs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[uuid.UUID, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    # -- state files -------------------------------------------------------
    def _path(self, job_id: uuid.UUID) -> Path:
        return self._dir / f"{job_id}{JOB_FILE_SUFFIX}"

    def _read(self, job_id: uuid.UUID) -> tuple[dict[str, Any], JobStatus] | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text())
            return raw.get("params", {}), JobStatus.model_validate(raw["status"])
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("job file %s is unreadable (%s)", path, exc)
            return None

    def _write(self, params: dict[str, Any], status: JobStatus) -> None:
        write_atomic(
            self._path(status.job_id),
            json.dumps(
                {"params": params, "status": status.model_dump(mode="json")}, indent=2
            ).encode(),
        )

    # -- queries -----------------------------------------------------------
    def status(self, job_id: uuid.UUID) -> JobStatus | None:
        found = self._read(job_id)
        if found is None:
            return None
        _, status = found
        return self._with_worker_stats(status)

    def _with_worker_stats(self, status: JobStatus) -> JobStatus:
        """Attach live worker CPU, and notice a worker that died without saying so.

        A worker killed by the OOM killer or a `kill -9` never gets to write `failed`. Without
        this the job would sit in `transferring` for ever and the reconciler would wait on it.
        """
        if status.is_terminal:
            return status
        proc = self._procs.get(status.job_id)
        if proc is None:
            return status
        code = proc.poll()
        if code is not None:
            fresh = self._read(status.job_id)
            current = fresh[1] if fresh else status
            if not current.is_terminal:
                current.stage = JobStage.FAILED
                current.error = f"the worker exited with status {code} without reporting"
                current.error_kind = JobErrorKind.INTERNAL
                current.finished_at = datetime.now(UTC)
                if fresh:
                    self._write(fresh[0], current)
                logger.error("job %s: worker vanished (exit %s)", status.job_id, code)
            return current
        try:
            status.worker_pid = proc.pid
            status.worker_cpu_percent = psutil.Process(proc.pid).cpu_percent(interval=None)
        except psutil.Error:
            pass
        return status

    def active(self) -> list[JobStatus]:
        """Every job the agent knows, freshest first. Reported on `/node/health`."""
        out: list[JobStatus] = []
        for path in sorted(self._dir.glob(f"*{JOB_FILE_SUFFIX}")):
            try:
                job_id = uuid.UUID(path.stem)
            except ValueError:
                continue
            status = self.status(job_id)
            if status is not None:
                out.append(status)
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    def _writer_for(self, slug: str) -> uuid.UUID | None:
        for status in self.active():
            if status.slug == slug and not status.is_terminal:
                return status.job_id
        return None

    # -- starting ----------------------------------------------------------
    def start(
        self,
        *,
        job_id: uuid.UUID,
        kind: JobKind,
        slug: str,
        params: dict[str, Any],
        expected_total_bytes: int | None = None,
    ) -> tuple[bool, JobStatus]:
        """Start a job, or return the existing one.

        Returns `(created, status)`. Idempotent on `job_id`: the control plane re-issues the
        current stage after a restart and must not get a second download for it (ADR-0005).
        """
        with self._lock:
            existing = self._read(job_id)
            if existing is not None:
                logger.info("job %s already exists; returning it unchanged", job_id)
                return False, self._with_worker_stats(existing[1])

            other = self._writer_for(slug)
            if other is not None:
                raise JobConflict(f"job {other} is already writing {slug}")

            if expected_total_bytes:
                required = expected_total_bytes + self._settings.disk_reserve_bytes
                free = self._store.free_bytes()
                if free < required:
                    raise InsufficientSpace(required, free)

            now = datetime.now(UTC)
            status = JobStatus(
                job_id=job_id,
                kind=kind,
                slug=slug,
                stage=JobStage.QUEUED,
                bytes_total=expected_total_bytes or 0,
                started_at=now,
                updated_at=now,
            )
            full_params = {
                **params,
                "store_dir": str(self._store.root),
                "cache_dir": self._settings.node_hf_cache_dir,
                "disk_reserve_bytes": self._settings.disk_reserve_bytes,
                "node_port": self._settings.node_listen_port,
            }
            self._write(full_params, status)
            self._spawn(job_id)
            return True, status

    def _spawn(self, job_id: uuid.UUID) -> None:
        env = dict(os.environ)
        # The Hugging Face token is passed ONLY here, into the child that needs it. The agent
        # never puts it in its own environment, so an unrelated library in the agent process
        # cannot pick it up (spec FR-005).
        hf = self._settings.hf_token.get_secret_value()
        if hf:
            env["HF_TOKEN"] = hf
        env.setdefault("HF_HOME", self._settings.node_hf_cache_dir)
        env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

        proc = subprocess.Popen(
            [sys.executable, "-m", "coire_node.worker", str(self._path(job_id))],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Its own session, so the agent's own restart does not take the worker with it.
            start_new_session=True,
        )
        self._procs[job_id] = proc
        logger.info("started worker pid %d for job %s", proc.pid, job_id)

    # -- cancelling and resuming -------------------------------------------
    def cancel(self, job_id: uuid.UUID) -> bool:
        found = self._read(job_id)
        if found is None:
            return False
        params, status = found
        proc = self._procs.pop(job_id, None)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        if not status.is_terminal:
            status.stage = JobStage.CANCELLED
            status.error_kind = JobErrorKind.CANCELLED
            status.finished_at = datetime.now(UTC)
            self._write(params, status)
        # A partial *pull* is kept: completed files are reused on retry. A partial *import* is
        # deleted, because a half-copied directory is not a resume point the origin agrees on.
        if status.kind is JobKind.IMPORT:
            self._store.delete(status.slug)
        return True

    def resume_all(self) -> int:
        """Re-spawn workers for jobs that were running when the agent stopped.

        This is what makes an interrupted pull survive a node reboot (spec edge case 3): the
        job file records the stage, and a fresh worker picks it up rather than the acquisition
        stalling until an admin notices.
        """
        resumed = 0
        for status in self.active():
            if status.is_terminal or status.job_id in self._procs:
                continue
            logger.info(
                "resuming job %s (%s %s) from stage %s",
                status.job_id,
                status.kind.value,
                status.slug,
                status.stage.value,
            )
            self._spawn(status.job_id)
            resumed += 1
        return resumed

    def shutdown(self) -> None:
        """Leave workers running. The agent is restartable; its jobs should not restart with
        it, and `resume_all` re-attaches on the way back up."""
        self._procs.clear()
