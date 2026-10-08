"""Owned native trainer lifecycle. A spawn intent is never a license to respawn."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training import TrainingReason
from coire_core.models.training_node import (
    CheckpointAcknowledgementDocument,
    DatasetInputGrant,
    NodeTrainingStatus,
    TrainingInputsRequest,
    TrainingLeaseRenewal,
    TrainingPauseRequest,
    TrainingPrepared,
    TrainingPrepareRequest,
    TrainingRankCollection,
    TrainingReconcileRequest,
    TrainingReconcileResult,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopReceipt,
    TrainingStopRequest,
)
from coire_core.settings import Settings
from coire_node.store import Store, write_atomic
from coire_node.training.components import TrainingComponents
from coire_node.training.distributed import JacclLaunch
from coire_node.training.journal import TrainingJournal
from coire_node.training.objectives import validate_sft_input
from coire_node.training.worker import (
    FrozenInputs,
    load_all_frozen_inputs,
    read_private,
    validate_frozen_inputs,
    verify_source,
)

tracer = trace.get_tracer("coire.node.training")
logger = logging.getLogger(__name__)
stops = metrics.get_meter("coire.node.training").create_counter("coire_training_node_stops_total")
preparation_outcomes = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_node_preparations_total"
)


class Processes(Protocol):
    def spawn(self, argv: list[str], env: dict[str, str]) -> tuple[int, float]: ...
    def discover(self, argv: list[str]) -> tuple[str, int | None, float | None]: ...
    def observe(self, argv: list[str], pid: int, created: float) -> str: ...
    def stop(self, argv: list[str], pid: int, created: float, deadline: float) -> str: ...


class NativeProcesses:
    def __init__(self) -> None:
        self.children: dict[int, subprocess.Popen[bytes]] = {}

    def spawn(self, argv: list[str], env: dict[str, str]) -> tuple[int, float]:
        child = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        self.children[child.pid] = child
        return child.pid, psutil.Process(child.pid).create_time()

    def discover(self, argv: list[str]) -> tuple[str, int | None, float | None]:
        matches: list[tuple[int, float]] = []
        uncertain = False
        for process in psutil.process_iter():
            try:
                if process.cmdline() == argv and os.getpgid(process.pid) == process.pid:
                    matches.append((process.pid, process.create_time()))
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError):
                uncertain = True
        if len(matches) == 1 and not uncertain:
            return "running", *matches[0]
        # Absence after half-spawn cannot prove the child wasn't about to start.
        return "unknown", None, None

    def observe(self, argv: list[str], pid: int, created: float) -> str:
        child = self.children.get(pid)
        if child is not None:
            child.poll()  # Reap owned children before group death inspection.
        try:
            process = psutil.Process(pid)
            if process.create_time() != created or os.getpgid(pid) != pid:
                return "unknown"
            if process.status() == psutil.STATUS_ZOMBIE:
                # The owned Popen/waitpid result proves this child's exit even
                # when macOS has already cleared the zombie's argv. A matching
                # PID alone remains insufficient, and group absence is checked below.
                if child is None or child.poll() is None:
                    return "unknown"
            elif process.cmdline() != argv:
                # Exit may also race the first status read. Never accept a live
                # argv mismatch or a reused birth/session as an owned dead child.
                if (
                    process.status() != psutil.STATUS_ZOMBIE
                    or child is None
                    or child.poll() is None
                ):
                    return "unknown"
            else:
                return "running"
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
        except (psutil.AccessDenied, PermissionError):
            return "unknown"
        # Leader absence alone is insufficient: a native group may retain children.
        for process in psutil.process_iter():
            try:
                if os.getpgid(process.pid) == pid:
                    return "unknown"
            except ProcessLookupError:
                continue
            except PermissionError:
                return "unknown"
        return "stopped"

    def stop(self, argv: list[str], pid: int, created: float, deadline: float) -> str:
        state = self.observe(argv, pid, created)
        if state != "running":
            return state
        members: dict[int, float] = {}
        for process in psutil.process_iter():
            try:
                if os.getpgid(process.pid) == pid:
                    members[process.pid] = process.create_time()
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError):
                return "unknown"

        def still_owned() -> bool:
            # A verified child permits group escalation after the leader exits.
            for member, birth in members.items():
                try:
                    if psutil.Process(member).create_time() == birth and os.getpgid(member) == pid:
                        return True
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    ProcessLookupError,
                    PermissionError,
                ):
                    continue
            return False

        try:
            os.killpg(pid, signal.SIGTERM)
            while time.monotonic() < deadline:
                state = self.observe(argv, pid, created)
                if state == "stopped":
                    return state
                # Escalation remains inside the five-second lane, not at its end.
                if time.monotonic() >= deadline - 3 and still_owned():
                    os.killpg(pid, signal.SIGKILL)
                time.sleep(0.05)
        except (ProcessLookupError, PermissionError):
            pass
        return self.observe(argv, pid, created)


class TrainingSupervisor:
    def __init__(
        self,
        journal: TrainingJournal,
        *,
        interpreter: Path,
        validate_ready: Callable[[TrainingPrepareRequest], None] | None = None,
        processes: Processes | None = None,
        store_root: Path = Path("/opt/coire/models"),
        artifact_root: Path | None = None,
        memory_available: Callable[[], int] | None = None,
        disk_available: Callable[[], int] | None = None,
        disk_floor: int = 20 * 1024**3,
        jaccl_hostfile: Path | None = None,
        jaccl_coordinator_port: int = 32323,
        otlp_endpoint: str | None = None,
    ) -> None:
        if (
            not interpreter.is_absolute()
            or not store_root.is_absolute()
            or (artifact_root is not None and not artifact_root.is_absolute())
        ):
            raise TrainingValidationError(
                "Trainer interpreter and store roots must be absolute node configuration"
            )
        self.journal = journal
        self.interpreter = interpreter
        self.otlp_endpoint = otlp_endpoint
        self.store_root = store_root
        self.artifact_root = artifact_root or journal.root / "artifacts"
        from coire_node.training.rejections import PrepareRejections

        self.rejections = PrepareRejections(journal, self.artifact_root)
        self.native_validation = validate_ready is None
        self.validate_ready = validate_ready or self.validate_native_inputs
        self.processes = processes or NativeProcesses()
        self.lock = asyncio.Lock()
        self.preparations: dict[str, dict[asyncio.Task[None], bool]] = {}
        self.memory_available = memory_available
        self.disk_available = disk_available
        self.disk_floor = disk_floor
        self.jaccl_hostfile = jaccl_hostfile
        self.jaccl_coordinator_port = jaccl_coordinator_port
        self.components = TrainingComponents(self.artifact_root, journal)

    def collective_launch(self, command: TrainingPrepareRequest) -> JacclLaunch:
        binding = command.collective
        if (
            binding is None
            or self.jaccl_hostfile is None
            or command.world_size != 2
            or binding.runtime_sha256 != command.resolved.runtime_sha256
            or binding.coordinator_port != self.jaccl_coordinator_port
        ):
            raise TrainingValidationError("Declared JACCL collective launch binding is unavailable")
        launch = JacclLaunch(self.jaccl_hostfile, binding.hostfile_sha256, binding.coordinator_port)
        launch.devices()
        return launch

    async def publish_collection(self, command: TrainingRankCollection) -> None:
        """Both descriptors and local bytes must verify before an owned mailbox is written."""
        async with self.lock:
            with self.journal.transaction():
                self.journal.accept(command)
                self.journal.scoped(command)
        for component in command.components:
            await asyncio.to_thread(self.components.verify, component)
        async with self.lock:
            with self.journal.transaction():
                value = self.journal.scoped(command)
                self.journal.accept(command)
                if (
                    value["liveness"] != "running"
                    or value["released"]
                    or value.get("update", 0) != command.components[0].update
                ):
                    raise TrainingConflict("Rank collection is not the current evaluated update")
                for component in command.components:
                    self.components.scope(component)
                name = f"collection-{command.artifact_id}.json"
                try:
                    prior = TrainingRankCollection.model_validate_json(
                        read_private(self.directory(command.attempt_id) / name, 64 * 1024**2)
                    )
                    if prior.components != command.components:
                        raise TrainingConflict("Rank collection descriptors are immutable")
                except FileNotFoundError:
                    pass
                self.publish_control(command.attempt_id, name, command.model_dump_json().encode())
                self.journal.receipt(command, "collected")

    async def submit_inputs(
        self, command: TrainingInputsRequest, *, settings: Settings
    ) -> TrainingPrepared:
        """Accept a bounded owned delivery; persist only its credential-free identity."""
        async with self.lock:
            with self.journal.transaction():
                value = self.journal.scoped(command)
                self.journal.accept(command)
                prepared = TrainingPrepareRequest.model_validate(value["prepare"])
                if value["released"] or value["spawn_nonce"] is not None:
                    raise TrainingConflict("Inputs require a current unstarted preparation")
                if {s.binding.dataset_id for s in command.sources} != {
                    s.dataset_id for s in prepared.resolved.datasets
                }:
                    raise TrainingConflict(
                        "Input delivery must cover the exact resolved source set"
                    )
                if len({s.binding.model_slug for s in command.sources}) != 1:
                    raise TrainingConflict("Source model bindings differ")
                for source in command.sources:
                    validate_frozen_inputs(
                        prepared,
                        FrozenInputs(
                            source.binding, source.split, source.analysis, Path("/ignored")
                        ),
                    )
                # Grant secrets never enter the SQLite command digest or restart record.
                if self.preparations.get(command.attempt_id):
                    return self.input_receipt(prepared, ready=False)
                complete = value.get("input_files") is not None or len(
                    value.get("input_sources", {})
                ) == len(command.sources)
                if not complete:
                    if self.disk_available is None:
                        raise TrainingValidationError("Native disk admission source unavailable")
                    disk = self.disk_available() - self.disk_floor
                    task = asyncio.create_task(
                        self._deliver_sources(prepared, command, settings, disk)
                    )
                    self.track_preparation(command.attempt_id, task, network=True)
                    return self.input_receipt(prepared, ready=False)
        await self.validate_assets(prepared)
        return self.input_receipt(prepared, ready=True)

    async def _deliver_sources(
        self,
        prepared: TrainingPrepareRequest,
        command: TrainingInputsRequest,
        settings: Settings,
        disk: int,
    ) -> None:
        for source in command.sources:
            await self._download_inputs(
                prepared,
                source.grant,
                FrozenInputs(source.binding, source.split, source.analysis, Path("/ignored")),
                settings=settings,
                disk_available=disk,
                transport=None,
                multi=len(command.sources) > 1,
            )
        await self.validate_assets(prepared)

    def input_receipt(self, command: TrainingPrepareRequest, *, ready: bool) -> TrainingPrepared:
        return TrainingPrepared(
            attempt_id=command.attempt_id,
            fence=command.fence,
            node=command.node,
            reservation_id=command.reservation_id,
            runtime_sha256=command.resolved.runtime_sha256,
            ready=ready,
            reason=None if ready else "analysis_pending",
        )

    async def aclose(self) -> None:
        """Finish private staging before closing its journal; native workers remain owned."""
        tasks = [task for owned in self.preparations.values() for task in owned]
        for owned in self.preparations.values():
            for task, network in owned.items():
                if network:
                    task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.journal.close()

    async def validate_assets(self, command: TrainingPrepareRequest) -> None:
        task = asyncio.create_task(asyncio.to_thread(self.validate_ready, command))
        if self.native_validation:
            self.track_preparation(command.attempt_id, task, network=False)
        await asyncio.shield(task)

    def committed_bytes(self) -> int:
        """Unknown owners retain their envelope; measured overages count too."""
        with self.journal.lock:
            total = 0
            for value in self.journal.records():
                if value["released"]:
                    continue
                measured = self.footprint(value)
                total += max(value["memory_bytes"], measured or 0)
            return total

    def held_disk_bytes(self, path: Path) -> int:
        with self.journal.lock:
            if path.stat().st_dev != self.journal.root.stat().st_dev:
                return 0
            return self.journal.held_bytes()[1]

    def footprint(self, value: dict[str, Any]) -> int | None:
        from coire_node.footprint import phys_footprint

        if (
            value["pid"] is None
            or self.processes.observe(self.argv(value), value["pid"], value["process_create_time"])
            != "running"
        ):
            return None
        try:
            return phys_footprint(value["pid"])
        except (OSError, psutil.Error):
            return None

    def statuses(self) -> list[NodeTrainingStatus]:
        return [
            self.observe(value["attempt_id"])
            for value in self.journal.records()
            if not value["released"]
        ]

    def track_preparation(
        self, attempt_id: str, task: asyncio.Task[None], *, network: bool
    ) -> None:
        tasks = self.preparations.setdefault(attempt_id, {})
        tasks[task] = network

        def finished(completed: asyncio.Task[None]) -> None:
            tasks.pop(completed, None)
            if completed.cancelled():
                preparation_outcomes.add(1, {"outcome": "cancelled"})
                return
            error = completed.exception()  # Consume errors even if the observer disconnected.
            preparation_outcomes.add(1, {"outcome": "failed" if error else "succeeded"})
            if error is not None:
                logger.warning(
                    "native training preparation failed",
                    extra={"attempt_id": attempt_id, "error_type": type(error).__name__},
                )

        task.add_done_callback(finished)

    def directory(self, attempt_id: str) -> Path:
        # Always resolve identity via a validated journal before constructing a path.
        command = TrainingPrepareRequest.model_validate(self.journal.get(attempt_id)["prepare"])
        path = self.journal.root / command.attempt_id
        if path.is_symlink():
            raise TrainingValidationError("Training execution directory is linked")
        path.mkdir(mode=0o700, exist_ok=True)
        if path.stat().st_mode & 0o077:
            raise TrainingValidationError("Training execution directory must be private")
        return path

    def argv(self, value: dict[str, Any]) -> list[str]:
        return [
            str(self.interpreter),
            "-m",
            "coire_node.training.worker",
            "--attempt",
            value["attempt_id"],
            "--owner",
            str(value["spawn_nonce"]),
            "--state-root",
            str(self.journal.root),
            "--store-root",
            str(self.store_root),
            "--artifact-root",
            str(self.artifact_root),
        ]

    def validate_native_inputs(self, command: TrainingPrepareRequest) -> None:
        if command.world_size != 1:
            self.collective_launch(command)
        sources = load_all_frozen_inputs(command, self.directory(command.attempt_id), self.journal)
        inputs = sources[0]
        from coire_node.training.datasets import load_analysis_tokenizer

        _, tokenizer_sha, template_sha, runtime_sha = load_analysis_tokenizer(
            Store(self.store_root).path_for(inputs.binding.model_slug), inputs.binding
        )
        if (
            tokenizer_sha != command.resolved.tokenizer_sha256
            or template_sha != command.resolved.template_sha256
            or runtime_sha != command.resolved.runtime_sha256
        ):
            raise TrainingConflict(
                "Native tokenizer/template/runtime differs from resolved execution"
            )
        validate_sft_input(
            Store(self.store_root).path_for(inputs.binding.model_slug),
            command.resolved.spec.parameterization,
            expected_manifest_sha256=command.resolved.base_manifest_sha256,
        )

    async def bind_inputs(
        self,
        command: TrainingPrepareRequest,
        inputs: FrozenInputs,
        *,
        disk_available: int,
        multi: bool = False,
    ) -> None:
        """Authenticated node caller stages existing core contracts, never a path on the wire."""
        tasks = self.preparations.setdefault(command.attempt_id, {})
        if any(not network for network in tasks.values()):
            raise TrainingConflict("Training input staging already has an owned preparation")
        task = asyncio.create_task(
            asyncio.to_thread(self._bind_inputs, command, inputs, disk_available, True)
            if multi
            else asyncio.to_thread(self._bind_inputs, command, inputs, disk_available)
        )
        self.track_preparation(command.attempt_id, task, network=False)
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                tasks.pop(task, None)

    def _bind_inputs(
        self,
        command: TrainingPrepareRequest,
        inputs: FrozenInputs,
        disk_available: int,
        multi: bool = False,
    ) -> None:
        validate_frozen_inputs(command, inputs)
        source_size = verify_source(inputs.source, inputs.binding.source_sha256)
        encoded = {
            "binding.json": inputs.binding.model_dump_json().encode(),
            "split.json": inputs.split.model_dump_json().encode(),
            "analysis.json": inputs.analysis.model_dump_json().encode(),
        }
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in encoded.items()}
        hashes["source.jsonl"] = inputs.binding.source_sha256
        key = str(inputs.binding.dataset_id)
        with self.journal.transaction():
            value = self.journal.scoped(command)
            prior = value.get("input_sources", {}).get(key) if multi else value.get("input_files")
            if prior is not None:
                if prior != hashes:
                    raise TrainingConflict("Frozen training input binding is immutable")
                return
        with self.journal.lock:
            self.journal.hold_input_bytes(
                command.attempt_id,
                source_size + sum(len(data) for data in encoded.values()),
                disk_available=self.disk_available() - self.disk_floor
                if self.disk_available
                else disk_available,
                source_id=key if multi else None,
            )
        directory = self.directory(command.attempt_id)
        if multi:
            directory = directory / key
            if directory.is_symlink():
                raise TrainingValidationError("Training input directory is linked")
            directory.mkdir(mode=0o700, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".inputs-", dir=directory))
        try:
            for name, data in encoded.items():
                write_atomic(staging / name, data)
            descriptor = os.open(inputs.source, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with (
                    os.fdopen(descriptor, "rb", closefd=False) as source,
                    (staging / "source.jsonl").open("xb") as target,
                ):
                    (staging / "source.jsonl").chmod(0o600)
                    size = 0
                    while chunk := source.read(64 * 1024):
                        size += len(chunk)
                        if size > source_size:
                            raise TrainingConflict("Frozen source changed during staging")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            finally:
                os.close(descriptor)
            verify_source(staging / "source.jsonl", inputs.binding.source_sha256)
            with self.journal.transaction():
                value = self.journal.scoped(command)
                if (
                    value["liveness"] != "prepared"
                    or value["spawn_nonce"] is not None
                    or value["released"]
                ):
                    raise TrainingConflict("Stopped or spawned attempt cannot bind inputs")
                prior = (
                    value.get("input_sources", {}).get(key) if multi else value.get("input_files")
                )
                if prior is not None and prior != hashes:
                    raise TrainingConflict("Concurrent input staging differs")
                for name in hashes:
                    os.replace(staging / name, directory / name)
                fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if multi:
                    value.setdefault("input_sources", {})[key] = hashes
                else:
                    value["input_files"] = hashes
                self.journal.save(value)
        finally:
            import shutil

            shutil.rmtree(staging)

    async def download_inputs(
        self,
        command: TrainingPrepareRequest,
        grant: DatasetInputGrant,
        inputs: FrozenInputs,
        *,
        settings: Settings,
        disk_available: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        tasks = self.preparations.setdefault(command.attempt_id, {})
        if tasks:
            raise TrainingConflict("Training input delivery already has an owned preparation")
        task = asyncio.create_task(
            self._download_inputs(
                command,
                grant,
                inputs,
                settings=settings,
                disk_available=disk_available,
                transport=transport,
            )
        )
        self.track_preparation(command.attempt_id, task, network=True)
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                tasks.pop(task, None)

    async def _download_inputs(
        self,
        command: TrainingPrepareRequest,
        grant: DatasetInputGrant,
        inputs: FrozenInputs,
        *,
        settings: Settings,
        disk_available: int,
        transport: httpx.AsyncBaseTransport | None,
        multi: bool = False,
    ) -> None:
        """The source path in `inputs` is ignored; generated local staging receives grant bytes."""
        validate_frozen_inputs(command, inputs)
        grant = DatasetInputGrant.model_validate(grant.model_dump(mode="json"))
        if (
            grant.node != command.node
            or grant.attempt_id != command.attempt_id
            or grant.analysis_id is not None
            or grant.dataset_id != inputs.binding.dataset_id
            or grant.source_sha256 != inputs.binding.source_sha256
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise TrainingConflict("Dataset grant differs from current training scope")
        origin = settings.training_input_api_url
        parsed = urlsplit(origin)
        if (
            parsed.hostname != settings.core_control_host
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise TrainingValidationError("Dataset source origin differs from declared core")
        directory = self.directory(command.attempt_id)
        metadata_bytes = sum(
            len(value.model_dump_json().encode())
            for value in (inputs.binding, inputs.split, inputs.analysis)
        )
        with self.journal.lock:
            self.journal.hold_input_bytes(
                command.attempt_id,
                2 * grant.max_bytes + metadata_bytes,
                disk_available=self.disk_available() - self.disk_floor
                if self.disk_available
                else disk_available,
                source_id=str(inputs.binding.dataset_id) if multi else None,
            )
        fd, name = tempfile.mkstemp(prefix=".download-", dir=directory)
        source_path = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                count, digest = 0, hashlib.sha256()
                async with (
                    asyncio.timeout(
                        max(0, min((grant.expires_at - datetime.now(UTC)).total_seconds(), 30))
                    ),
                    httpx.AsyncClient(
                        timeout=30, follow_redirects=False, trust_env=False, transport=transport
                    ) as client,
                    client.stream(
                        "GET",
                        f"{origin.rstrip('/')}/api/v1/internal/training/datasets/{grant.dataset_id}/content",
                        headers={
                            "Authorization": "Bearer " + settings.node_token.get_secret_value(),
                            "X-Coire-Node": command.node,
                            "X-Coire-Dataset-Grant": grant.secret,
                        },
                    ) as response,
                ):
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise TrainingValidationError("Compressed dataset delivery is unsupported")
                    async for chunk in response.aiter_bytes(64 * 1024):
                        count += len(chunk)
                        if count > grant.max_bytes or grant.expires_at <= datetime.now(UTC):
                            raise TrainingValidationError("Dataset delivery exceeds grant bounds")
                        digest.update(chunk)
                        await asyncio.to_thread(output.write, chunk)
                await asyncio.to_thread(output.flush)
                await asyncio.to_thread(os.fsync, output.fileno())
            if not count or digest.hexdigest() != grant.source_sha256:
                raise TrainingConflict("Dataset delivery digest differs")
            # max_bytes is a ceiling; retain conservative unused grant capacity in the hold.
            await self.bind_inputs(
                command,
                FrozenInputs(inputs.binding, inputs.split, inputs.analysis, source_path),
                disk_available=disk_available,
                multi=multi,
            )
        finally:
            await asyncio.to_thread(source_path.unlink, missing_ok=True)

    async def acknowledge_checkpoint(self, command: CheckpointAcknowledgementDocument) -> None:
        async with self.lock:
            with self.journal.transaction():
                replay = self.journal.accept(command)
                value = self.journal.scoped(command)
                prepared = TrainingPrepareRequest.model_validate(value["prepare"])
                if command.schema_version != prepared.resolved.spec.schema_version:
                    raise TrainingConflict(
                        "Checkpoint decision differs from negotiated training version"
                    )
                if replay is not None:
                    return
                from coire_node.training.checkpoints import CheckpointStore

                manifest = CheckpointStore(self.artifact_root).manifest(command.checkpoint_id)
                if (
                    manifest.canonical_sha256() != command.manifest_sha256
                    or manifest.attempt_id != command.attempt_id
                    or manifest.job_id != command.job_id
                    or manifest.fence != command.fence
                    or manifest.update != command.update
                ):
                    raise TrainingConflict(
                        "Checkpoint acknowledgement differs from staged immutable state"
                    )
                if value["liveness"] != "running" or datetime.fromisoformat(
                    value["lease_expires_at"]
                ) <= datetime.now(UTC):
                    raise TrainingConflict(
                        "Stopped or expired attempt cannot accept checkpoint commitment"
                    )
                self.publish_control(
                    command.attempt_id,
                    f"commit-{command.checkpoint_id}.json",
                    command.model_dump_json().encode(),
                )
                value["latest_manifest_sha256"] = command.manifest_sha256
                value["latest_checkpoint_id"] = str(command.checkpoint_id)
                self.journal.save(value)
                self.journal.receipt(command, "committed")

    def publish_control(self, attempt_id: str, name: str, data: bytes) -> None:
        directory = self.directory(attempt_id)
        write_atomic(directory / name, data)
        for path in (directory, self.journal.root):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def references_artifact(self, identity: uuid.UUID) -> bool:
        """Protect actual native recovery/commit references, not every historical file."""
        with self.journal.lock:
            for record in self.journal.records():
                if record["released"]:
                    continue
                # Older live journals cannot prove which callback file is in use.
                if record.get("artifact_tracking_version") != 1:
                    return True
                prepared = TrainingPrepareRequest.model_validate(record["prepare"])
                if prepared.resume_checkpoint_id == identity or str(identity) in {
                    record.get("latest_staged_artifact_id"),
                    record.get("latest_checkpoint_id"),
                }:
                    return True
            return False

    async def prepare(
        self,
        command: TrainingPrepareRequest,
        *,
        memory_available: int | None = None,
        disk_available: int | None = None,
        disk_floor: int | None = None,
    ) -> TrainingPrepared:
        command = TrainingPrepareRequest.model_validate(command.model_dump(mode="json"))
        async with self.lock:
            with self.journal.lock:
                rejected = self.rejections.read(command.attempt_id)
                if rejected is not None:
                    if rejected != command:
                        raise TrainingConflict("Rejected preparation intent changed")
                    raise TrainingConflict("Training preparation was durably rejected")
                try:
                    if command.world_size == 2 and self.native_validation:
                        self.collective_launch(command)
                    memory = self.memory_available() if self.memory_available else memory_available
                    disk = self.disk_available() if self.disk_available else disk_available
                    if memory is None or disk is None:
                        raise TrainingValidationError("Native admission sources unavailable")
                    self.journal.prepare(
                        command,
                        memory_available=memory,
                        disk_available=disk,
                        disk_floor=self.disk_floor if disk_floor is None else disk_floor,
                    )
                except (TrainingConflict, TrainingValidationError):
                    with tracer.start_as_current_span("coire.node.training.prepare.reject"):
                        self.rejections.record(command)
                    raise
        # Stage inputs after acquiring holds. Missing inputs are a truthful pending receipt.
        ready = True
        reason: TrainingReason | None = None
        if self.preparations.get(command.attempt_id) or (
            self.native_validation
            and self.journal.get(command.attempt_id).get("input_files") is None
            and len(self.journal.get(command.attempt_id).get("input_sources", {}))
            != len(command.resolved.datasets)
        ):
            ready, reason = False, "analysis_pending"
        else:
            await self.validate_assets(command)
        value = self.journal.get(command.attempt_id)
        if value["liveness"] == "stopped" or value["released"]:
            ready, reason = False, value["reason"] or "cancelled"
        return TrainingPrepared(
            attempt_id=command.attempt_id,
            fence=command.fence,
            node=command.node,
            reservation_id=command.reservation_id,
            runtime_sha256=command.resolved.runtime_sha256,
            ready=ready,
            reason=reason,
        )

    async def reconcile(self, request: TrainingReconcileRequest) -> TrainingReconcileResult:
        async with self.lock:
            result = TrainingReconcileResult()
            expected_ids = {item.attempt_id for item in request.expected}
            for expected in request.expected:
                try:
                    value = self.journal.get(expected.attempt_id)
                except TrainingConflict:
                    result.unknown.append(expected.attempt_id)
                    continue
                prepared = TrainingPrepareRequest.model_validate(value["prepare"])
                if (
                    expected.fence != prepared.fence
                    or value["spawn_nonce"] != str(expected.spawn_nonce)
                    or (expected.pid is not None and expected.pid != value["pid"])
                    or (
                        expected.process_create_time is not None
                        and expected.process_create_time != value["process_create_time"]
                    )
                ):
                    result.unknown.append(expected.attempt_id)
                    continue
                status = self.observe(expected.attempt_id)
                if status.liveness == "running":
                    result.adopted.append(status)
                elif status.liveness == "stopped":
                    result.dead.append(expected.attempt_id)
                else:
                    result.unknown.append(expected.attempt_id)
            for value in self.journal.records():
                if value["attempt_id"] not in expected_ids and not value["released"]:
                    status = self.observe(value["attempt_id"])
                    result.orphans.append(status.model_copy(update={"liveness": "orphan"}))
            return TrainingReconcileResult.model_validate(result.model_dump(mode="json"))

    def observe(self, attempt_id: str) -> NodeTrainingStatus:
        with self.journal.lock:
            rejected = self.rejections.read(attempt_id)
            if rejected is not None:
                return NodeTrainingStatus(
                    attempt_id=attempt_id,
                    job_id=rejected.job_id,
                    fence=rejected.fence,
                    node=rejected.node,
                    liveness="stopped",
                    update=0,
                    lease_expires_at=rejected.lease_expires_at,
                )
        with self.journal.transaction():
            value = self.journal.get(attempt_id)
            if value["spawn_nonce"] is not None and not value.get("cleanup", {}).get("receipt"):
                argv = self.argv(value)
                if value["pid"] is None:
                    pid: int | None
                    created: float | None
                    try:
                        birth = TrainingStartReceipt.model_validate_json(
                            read_private(self.directory(attempt_id) / "birth.json", 1024**2)
                        )
                        prepared = TrainingPrepareRequest.model_validate(value["prepare"])
                        if (
                            birth.attempt_id != prepared.attempt_id
                            or birth.fence != prepared.fence
                            or birth.reservation_id != prepared.reservation_id
                        ):
                            raise TrainingConflict("Worker birth differs from spawn intent")
                        state = self.processes.observe(argv, birth.pid, birth.process_create_time)
                        pid, created = birth.pid, birth.process_create_time
                    except FileNotFoundError:
                        state, pid, created = self.processes.discover(argv)
                    value.update(liveness=state, pid=pid, process_create_time=created)
                else:
                    value["liveness"] = self.processes.observe(
                        argv, value["pid"], value["process_create_time"]
                    )
                self.journal.save(value)
            prepared = TrainingPrepareRequest.model_validate(value["prepare"])
            return NodeTrainingStatus(
                attempt_id=attempt_id,
                job_id=prepared.job_id,
                fence=prepared.fence,
                node=prepared.node,
                liveness=value["liveness"],
                pid=value["pid"],
                process_create_time=value["process_create_time"],
                update=value.get("update", 0),
                lease_expires_at=value["lease_expires_at"],
                reason=value["reason"],
                latest_manifest_sha256=value.get("latest_manifest_sha256"),
                footprint_bytes=self.footprint(value),
            )

    def release_stopped_attempt(self, attempt_id: str) -> None:
        with self.journal.lock:
            if self.rejections.read(attempt_id) is None:
                self.journal.release_after_death(attempt_id)

    async def start(self, command: TrainingStartRequest) -> TrainingStartReceipt:
        with self.journal.lock:
            if self.rejections.read(command.attempt_id) is not None:
                raise TrainingConflict("Rejected training cannot start")
        if self.preparations.get(command.attempt_id):
            raise TrainingConflict("Training inputs are still being prepared")
        prepared_input = TrainingPrepareRequest.model_validate(
            self.journal.get(command.attempt_id)["prepare"]
        )
        await self.validate_assets(prepared_input)
        async with self.lock:
            with tracer.start_as_current_span("coire.node.training.start"):
                with self.journal.transaction():
                    replay = self.journal.accept(command)
                    value = self.journal.scoped(command)
                    prepared = TrainingPrepareRequest.model_validate(value["prepare"])
                    if command.prepared_command_id != prepared.command_id:
                        raise TrainingConflict("Start targets another preparation")
                    if replay:
                        return TrainingStartReceipt.model_validate_json(replay)
                    if value["released"]:
                        raise TrainingConflict("Training reservation is released")
                    if value["spawn_nonce"] is not None:
                        if value["spawn_nonce"] != str(command.spawn_nonce):
                            raise TrainingConflict("Spawn nonce is immutable")
                        if (
                            value["pid"] is not None
                            and self.processes.observe(
                                self.argv(value), value["pid"], value["process_create_time"]
                            )
                            == "running"
                        ):
                            receipt = TrainingStartReceipt(
                                attempt_id=command.attempt_id,
                                fence=command.fence,
                                pid=value["pid"],
                                process_create_time=value["process_create_time"],
                                reservation_id=prepared.reservation_id,
                            )
                            self.journal.receipt(command, receipt.model_dump_json())
                            return receipt
                        raise TrainingConflict(
                            "Spawn intent requires reconciliation, never replay spawn"
                        )
                    if value["liveness"] != "prepared" or value["reason"] is not None:
                        raise TrainingConflict("Stopped preparation cannot start an execution")
                    if command.world_size == 2:
                        self.collective_launch(prepared)
                    value.update(spawn_nonce=str(command.spawn_nonce), liveness="unknown")
                    value["lease_expires_at"] = command.lease_expires_at.isoformat()
                    self.journal.save(value)
                    self.publish_control(
                        command.attempt_id, "prepare.json", prepared.model_dump_json().encode()
                    )
                    self.publish_control(
                        command.attempt_id, "lease.json", command.model_dump_json().encode()
                    )
                # Intent commit precedes Popen. Failure here retains the unknown hold.
                env = {
                    key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ
                }
                env.update(
                    HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1"
                )
                if self.otlp_endpoint:
                    env["OTLP_ENDPOINT"] = self.otlp_endpoint
                if prepared.world_size == 2:
                    env.update(
                        self.collective_launch(prepared).environment(
                            prepared, self.directory(prepared.attempt_id)
                        )
                    )
                pid, created = self.processes.spawn(self.argv(value), env)
                receipt = TrainingStartReceipt(
                    attempt_id=command.attempt_id,
                    fence=command.fence,
                    pid=pid,
                    process_create_time=created,
                    reservation_id=prepared.reservation_id,
                )
                with self.journal.transaction():
                    value.update(pid=pid, process_create_time=created, liveness="running")
                    self.journal.save(value)
                    self.journal.receipt(command, receipt.model_dump_json())
                logger.info(
                    "owned training worker started",
                    extra={"job_id": command.job_id, "attempt_id": command.attempt_id, "pid": pid},
                )
                return receipt

    async def renew(self, command: TrainingLeaseRenewal) -> None:
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(self._renew, command))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Retain command serialization until the durable write finishes.
                await task
                raise

    def _renew(self, command: TrainingLeaseRenewal) -> None:
        with self.journal.transaction():
            replay = self.journal.accept(command)
            value = self.journal.scoped(command)
            if replay is not None:
                return
            old = datetime.fromisoformat(value["lease_expires_at"])
            if (
                old <= datetime.now(UTC)
                or command.lease_expires_at <= old
                or value["liveness"] != "running"
            ):
                raise TrainingConflict("Lease cannot resurrect, shorten or authorize stopped work")
            value["lease_expires_at"] = command.lease_expires_at.isoformat()
            self.publish_control(
                command.attempt_id, "renew.json", command.model_dump_json().encode()
            )
            self.journal.save(value)
            self.journal.receipt(command, "renewed")

    async def pause(self, command: TrainingPauseRequest) -> None:
        async with self.lock:
            with self.journal.transaction():
                replay = self.journal.accept(command)
                value = self.journal.scoped(command)
                if replay is not None:
                    return
                if value["liveness"] != "running":
                    raise TrainingConflict("Only a running attempt can pause")
                if value["pause_deadline"] is None:
                    value["pause_deadline"] = (
                        datetime.now(UTC) + timedelta(seconds=60)
                    ).isoformat()
                    value["reason"] = command.reason
                self.publish_control(
                    command.attempt_id, "pause.json", command.model_dump_json().encode()
                )
                self.journal.save(value)
                self.journal.receipt(command, "pause_requested")

    async def stop(self, command: TrainingStopRequest) -> TrainingStopReceipt:
        command = TrainingStopRequest.model_validate(command.model_dump(mode="json"))
        with self.journal.lock:
            rejected = self.rejections.read(command.attempt_id)
            if rejected is None and self.rejections.pristine(command.attempt_id):
                self.rejections.record_stop(command)
                rejected = self.rejections.read(command.attempt_id)
            if rejected is not None:
                if any(
                    getattr(command, field) != getattr(rejected, field)
                    for field in (
                        "job_id",
                        "attempt_id",
                        "fence",
                        "node",
                        "rank",
                        "world_size",
                        "request_sha256",
                    )
                ):
                    raise TrainingConflict("Rejected training stop scope differs")
                with self.journal.transaction():
                    prior = self.journal.accept(command, require_lease=False)
                    if prior:
                        return TrainingStopReceipt.model_validate_json(prior)
                    receipt = TrainingStopReceipt(
                        attempt_id=command.attempt_id,
                        fence=command.fence,
                        node=command.node,
                        stopped=True,
                        observed_at=datetime.now(UTC),
                    )
                    self.journal.receipt(command, receipt.model_dump_json())
                return receipt
        deadline = time.monotonic() + 5
        async with self.lock:
            with self.journal.transaction():
                replay = self.journal.accept(command, require_lease=False)
                value = self.journal.scoped(command)
                if replay:
                    return TrainingStopReceipt.model_validate_json(replay)
                value["reason"] = command.reason
                if value["spawn_nonce"] is None:
                    value["liveness"] = "stopping"
                self.journal.save(value)
            preparation_complete = True
            tasks = self.preparations.get(command.attempt_id, {})
            for task, network in list(tasks.items()):
                if network:
                    task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=max(0, deadline - time.monotonic())
                    )
                except TimeoutError:
                    preparation_complete = False
                except asyncio.CancelledError:
                    if not task.done():
                        raise
                except (TrainingConflict, TrainingValidationError, httpx.HTTPError):
                    pass
                finally:
                    if task.done():
                        tasks.pop(task, None)
            self.observe(command.attempt_id)
            value = self.journal.get(command.attempt_id)
            state = value["liveness"]
            if value["pid"] is not None:
                state = await asyncio.to_thread(
                    self.processes.stop,
                    self.argv(value),
                    value["pid"],
                    value["process_create_time"],
                    deadline,
                )
            elif value["spawn_nonce"] is None:
                state = "stopped" if preparation_complete else "unknown"
            with self.journal.transaction():
                value["liveness"] = state
                self.journal.save(value)
                receipt = TrainingStopReceipt(
                    attempt_id=command.attempt_id,
                    fence=command.fence,
                    node=command.node,
                    pid=value["pid"],
                    process_create_time=value["process_create_time"],
                    stopped=state == "stopped",
                    observed_at=datetime.now(UTC),
                )
                self.journal.receipt(command, receipt.model_dump_json())
            stops.add(1, {"state": state})
            logger.info(
                "training stop observed",
                extra={"job_id": command.job_id, "attempt_id": command.attempt_id, "state": state},
            )
            return receipt

    async def watchdog(self) -> None:
        """Run in the native node service; worker has a separate lease guard too."""
        while True:
            for value in await asyncio.to_thread(self.journal.records):
                if value["released"]:
                    continue
                try:
                    status = await asyncio.to_thread(self.observe, value["attempt_id"])
                except (TrainingConflict, TrainingValidationError, OSError, psutil.Error):
                    logger.error(
                        "training ownership unresolved", extra={"attempt_id": value["attempt_id"]}
                    )
                    continue
                if status.liveness == "stopped":
                    await asyncio.to_thread(self.journal.release_after_death, value["attempt_id"])
                    continue
                now = datetime.now(UTC)
                expired = datetime.fromisoformat(value["lease_expires_at"]) <= now
                pause = value["pause_deadline"]
                if expired or (pause is not None and datetime.fromisoformat(pause) <= now):
                    prepared = TrainingPrepareRequest.model_validate(value["prepare"])
                    try:
                        await self.stop(
                            TrainingStopRequest(
                                **{
                                    **prepared.model_dump(
                                        mode="json",
                                        exclude={
                                            "resolved",
                                            "reservation_id",
                                            "disk_reservation_id",
                                            "resume_manifest_sha256",
                                            "resume_checkpoint_id",
                                            "collective",
                                        },
                                    ),
                                    "command_id": str(uuid.uuid4()),
                                    "reason": "lease_expired" if expired else value["reason"],
                                },
                            )
                        )
                    except (TrainingConflict, TrainingValidationError, OSError, psutil.Error):
                        logger.error(
                            "training watchdog could not prove stop",
                            extra={"attempt_id": value["attempt_id"]},
                        )
            await asyncio.sleep(0.5)
