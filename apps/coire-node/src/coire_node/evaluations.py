"""Bounded node-owned evaluation scratch in the existing hardened run workspace root."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.evaluation_suites import template
from coire_core.models.evaluation import (
    EvaluationInputReceipt,
    EvaluationPressureStop,
    EvaluationWorkload,
    EvaluationWorkspacePrepare,
    EvaluationWorkspaceReceipt,
    canonical_digest,
)
from coire_core.settings import Settings
from coire_node.store import write_atomic
from coire_node.workspaces import WorkspaceError

tracer = trace.get_tracer("coire.node.evaluation")
logger = logging.getLogger(__name__)
operations = metrics.get_meter("coire.node.evaluation").create_counter(
    "coire_evaluation_workspace_operations_total"
)


class EvaluationWorkspaceManager:
    def __init__(self, settings: Settings) -> None:
        root = Path(settings.run_workspace_root)
        if not root.is_absolute() or root.is_symlink():
            raise WorkspaceError("evaluation_workspace_invalid", "workspace root is unsafe")
        self.root = root.resolve()
        self.settings = settings
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def _path(self, run_id: uuid.UUID, *, output: bool = False) -> Path:
        if not isinstance(run_id, uuid.UUID):
            raise WorkspaceError(
                "evaluation_workspace_invalid", "workspace requires generated UUID"
            )
        path = self.root / f"{'eval-output' if output else 'eval'}-{run_id}"
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise WorkspaceError("evaluation_workspace_invalid", "workspace path is unsafe")
        return path

    @staticmethod
    def _read(path: Path, limit: int) -> bytes:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > limit
                ):
                    raise ValueError
                data = source.read(limit + 1)
                if len(data) != metadata.st_size:
                    raise ValueError
                return data
        except (OSError, ValueError) as error:
            raise WorkspaceError(
                "evaluation_workspace_invalid", "workspace receipt is unsafe"
            ) from error

    async def prepare(self, command: EvaluationWorkspacePrepare) -> EvaluationWorkspaceReceipt:
        workload = command.workload
        if template(workload.suite.template.template_id) != workload.suite.template:
            raise WorkspaceError(
                "evaluation_catalog_invalid", "suite differs from installed catalog"
            )
        if workload.deadline <= datetime.now(UTC):
            raise WorkspaceError("evaluation_deadline_elapsed", "evaluation deadline elapsed")
        async with self._locks.setdefault(workload.run_id, asyncio.Lock()):
            with tracer.start_as_current_span(
                "coire.node.evaluation.prepare",
                record_exception=False,
                set_status_on_exception=False,
            ):
                receipt = await asyncio.to_thread(self._prepare, command)
            operations.add(1, {"operation": "prepare"})
            logger.info(
                "evaluation workspace prepared",
                extra={"run_id": str(workload.run_id), "evaluation_id": workload.evaluation_id},
            )
            return receipt

    def _prepare(self, command: EvaluationWorkspacePrepare) -> EvaluationWorkspaceReceipt:
        workload = command.workload
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        workspace = self._path(workload.run_id)
        output = self._path(workload.run_id, output=True)
        if workspace.exists():
            control = workspace / ".coire"
            if control.is_symlink():
                raise WorkspaceError("evaluation_workspace_invalid", "control path is unsafe")
            receipt = EvaluationWorkspaceReceipt.model_validate_json(
                self._read(control / "receipt.json", 8192)
            )
            if (
                receipt.request_sha256 != command.request_sha256
                or self._read(control / "request.json", 256 * 1024)
                != workload.model_dump_json().encode()
            ):
                raise WorkspaceError(
                    "workspace_prepare_conflict", "prepared evaluation differs from request"
                )
            # Ownership was published atomically; a crash before output creation is recoverable.
            output.mkdir(mode=0o700, exist_ok=True)
            return receipt
        if output.exists():
            raise WorkspaceError("workspace_prepare_conflict", "evaluation output already exists")
        receipt = EvaluationWorkspaceReceipt.model_validate(
            {
                "evaluation_id": workload.evaluation_id,
                "attempt_id": workload.attempt_id,
                "run_id": workload.run_id,
                "fence": workload.fence,
                "node": self.settings.node_name,
                "workspace_ref": workspace.name,
                "output_ref": output.name,
                "request_sha256": command.request_sha256,
                "suite_sha256": workload.suite.content_sha256,
                "agent_version": workload.target.runtime.harness_version,
            }
        )
        staged = Path(tempfile.mkdtemp(prefix=".eval-stage-", dir=self.root))
        try:
            control = staged / ".coire"
            control.mkdir(mode=0o700)
            (control / "inputs").mkdir(mode=0o700)
            write_atomic(control / "request.json", workload.model_dump_json().encode())
            write_atomic(control / "receipt.json", receipt.model_dump_json().encode())
            os.rename(staged, workspace)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            output.mkdir(mode=0o700)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return receipt

    async def stage_input(
        self, run_id: uuid.UUID, request_sha256: str, name: str, data: bytes
    ) -> EvaluationInputReceipt:
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            with tracer.start_as_current_span(
                "coire.node.evaluation.stage_input",
                record_exception=False,
                set_status_on_exception=False,
            ):
                receipt = await asyncio.to_thread(
                    self._stage_input, run_id, request_sha256, name, data
                )
            operations.add(1, {"operation": "stage_input"})
            return receipt

    def _stage_input(
        self, run_id: uuid.UUID, request_sha256: str, name: str, data: bytes
    ) -> EvaluationInputReceipt:
        import hashlib

        control = self._path(run_id) / ".coire"
        if control.is_symlink() or (control / "inputs").is_symlink():
            raise WorkspaceError("evaluation_workspace_invalid", "input path is unsafe")
        receipt = EvaluationWorkspaceReceipt.model_validate_json(
            self._read(control / "receipt.json", 8192)
        )
        workload = EvaluationWorkload.model_validate_json(
            self._read(control / "request.json", 256 * 1024)
        )
        declared = next((item for item in workload.input_files if item.name == name), None)
        if declared is None or receipt.request_sha256 != request_sha256 or receipt.run_id != run_id:
            raise WorkspaceError(
                "workspace_prepare_conflict", "input is undeclared or fence differs"
            )
        if len(data) != declared.bytes or hashlib.sha256(data).hexdigest() != declared.sha256:
            raise WorkspaceError(
                "evaluation_workspace_invalid", "input byte count or digest differs"
            )
        path = control / "inputs" / declared.name
        if path.exists() or path.is_symlink():
            if self._read(path, declared.bytes) != data:
                raise WorkspaceError("workspace_prepare_conflict", "staged input differs")
        else:
            write_atomic(path, data)
        return EvaluationInputReceipt(
            run_id=run_id,
            request_sha256=request_sha256,
            name=declared.name,
            sha256=declared.sha256,
            bytes=declared.bytes,
        )

    def workload(self, run_id: uuid.UUID, request_sha256: str) -> EvaluationWorkload:
        control = self._path(run_id) / ".coire"
        if control.is_symlink():
            raise WorkspaceError("evaluation_workspace_invalid", "control path is unsafe")
        workload = EvaluationWorkload.model_validate_json(
            self._read(control / "request.json", 256 * 1024)
        )
        if workload.run_id != run_id or canonical_digest(workload) != request_sha256:
            raise WorkspaceError("workspace_prepare_conflict", "workspace input identity differs")
        return workload

    def validate_inputs(self, run_id: uuid.UUID) -> None:
        import hashlib

        control = self._path(run_id) / ".coire"
        if control.is_symlink() or (control / "inputs").is_symlink():
            raise WorkspaceError("evaluation_workspace_invalid", "input path is unsafe")
        workload = EvaluationWorkload.model_validate_json(
            self._read(control / "request.json", 256 * 1024)
        )
        for declared in workload.input_files:
            data = self._read(control / "inputs" / declared.name, declared.bytes)
            if len(data) != declared.bytes or hashlib.sha256(data).hexdigest() != declared.sha256:
                raise WorkspaceError(
                    "evaluation_workspace_invalid", "staged input differs from receipt"
                )

    async def cleanup(self, run_id: uuid.UUID, request_sha256: str) -> None:
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            with tracer.start_as_current_span(
                "coire.node.evaluation.cleanup",
                record_exception=False,
                set_status_on_exception=False,
            ):
                await asyncio.to_thread(self._cleanup, run_id, request_sha256)
            operations.add(1, {"operation": "cleanup"})
            logger.info("evaluation workspace cleaned", extra={"run_id": str(run_id)})

    def _cleanup(self, run_id: uuid.UUID, request_sha256: str) -> None:
        workspace = self._path(run_id)
        output = self._path(run_id, output=True)
        if not workspace.exists():
            if output.exists():
                raise WorkspaceError(
                    "workspace_cleanup_conflict", "cleanup ownership is unavailable"
                )
            return
        control = workspace / ".coire"
        if control.is_symlink():
            raise WorkspaceError("evaluation_workspace_invalid", "control path is unsafe")
        receipt = EvaluationWorkspaceReceipt.model_validate_json(
            self._read(control / "receipt.json", 8192)
        )
        if receipt.run_id != run_id or receipt.request_sha256 != request_sha256:
            raise WorkspaceError("workspace_cleanup_conflict", "cleanup ownership differs")
        if output.exists():
            shutil.rmtree(output)
        shutil.rmtree(workspace)

    async def stop_pressure(self, command: EvaluationPressureStop) -> None:
        async with self._locks.setdefault(command.run_id, asyncio.Lock()):
            with tracer.start_as_current_span(
                "coire.node.evaluation.stop_pressure",
                record_exception=False,
                set_status_on_exception=False,
            ):
                await asyncio.to_thread(self._stop_pressure, command)
            operations.add(1, {"operation": "stop_pressure"})
            logger.info("evaluation pressure stopped", extra={"run_id": str(command.run_id)})

    def _stop_pressure(self, command: EvaluationPressureStop) -> None:
        control = self._path(command.run_id) / ".coire"
        if control.is_symlink():
            raise WorkspaceError("evaluation_workspace_invalid", "control path is unsafe")
        receipt = EvaluationWorkspaceReceipt.model_validate_json(
            self._read(control / "receipt.json", 8192)
        )
        workload = EvaluationWorkload.model_validate_json(
            self._read(control / "request.json", 256 * 1024)
        )
        if (
            receipt.request_sha256 != command.request_sha256
            or workload.pressure is None
            or workload.pressure.measurement_id != command.measurement_id
        ):
            raise WorkspaceError("workspace_prepare_conflict", "pressure stop ownership differs")
        write_atomic(control / "measurement-stop.json", command.model_dump_json().encode())
