"""Reserved native probes. Local probe saves are measurements, never recovery commits."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import logging
import os
import platform
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import psutil
from opentelemetry import metrics, trace
from pydantic import BaseModel, TypeAdapter

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgement,
    CheckpointWorkerState,
    NodeTrainingStatus,
    TrainingArtifactManifest,
    TrainingMeasurementCapabilities,
    TrainingMeasurementNodeStatus,
    TrainingMeasurementObservation,
    TrainingMeasurementPrepare,
    TrainingMeasurementRankCheckpoint,
    TrainingMeasurementRankSet,
    TrainingPrepared,
    TrainingPrepareRequest,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopReceipt,
    TrainingStopRequest,
)
from coire_core.models.training_types import TrainingId
from coire_core.settings import get_settings
from coire_node.footprint import phys_footprint
from coire_node.metrics import read_thermal_state
from coire_node.store import Store, write_atomic
from coire_node.training.checkpoints import CheckpointStore, _fsync_directory
from coire_node.training.components import descriptor, verify_directory
from coire_node.training.datasets import load_analysis_tokenizer
from coire_node.training.distributed import BareCollective, DeadlineGuardian, JacclLaunch
from coire_node.training.journal import TrainingJournal, command_digest
from coire_node.training.objectives import load_sft_runtime, validate_sft_input
from coire_node.training.process_inventory import attempt_process_absent
from coire_node.training.supervisor import TrainingSupervisor
from coire_node.training.worker import (
    FrozenInputs,
    PrivateControl,
    compile_samples,
    load_all_frozen_inputs,
    make_optimizer,
    payload_sha256,
    read_private,
    resolved_digest,
    run_sft,
)

tracer = trace.get_tracer("coire.node.training.measurement")
logger = logging.getLogger(__name__)
outcomes = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_measurement_outcomes_total"
)

RANK_FRAME_BYTES = 4096


def measurement_hook_available() -> bool:
    """Do not substitute a durable manifest/ack for local measurement components."""
    return "measurement_checkpoint" in inspect.signature(run_sft).parameters


class MeasurementRankExchange(Protocol):
    def exchange(self, local: TrainingMeasurementRankCheckpoint) -> TrainingMeasurementRankSet: ...


class BareMeasurementRankExchange:
    """Only bounded inert rank measurement JSON traverses this bare CPU collective."""

    def __init__(self, collective: BareCollective) -> None:
        self.collective = collective

    def exchange(self, local: TrainingMeasurementRankCheckpoint) -> TrainingMeasurementRankSet:
        encoded = local.model_dump_json().encode()
        if len(encoded) > RANK_FRAME_BYTES - 4:
            raise TrainingValidationError("Rank measurement metadata exceeds collective frame")
        frame = len(encoded).to_bytes(4, "big") + encoded
        frame += b"\x00" * (RANK_FRAME_BYTES - len(frame))
        mx = self.collective.mx
        gathered = mx.distributed.all_gather(
            mx.array(list(frame), dtype=mx.uint8), group=self.collective.group, stream=mx.cpu
        )
        mx.eval(gathered)
        values = bytes(gathered.tolist())
        if len(values) != RANK_FRAME_BYTES * 2:
            raise TrainingConflict("Bare measurement collective did not return exactly two frames")
        ranks = []
        for offset in (0, RANK_FRAME_BYTES):
            frame = values[offset : offset + RANK_FRAME_BYTES]
            size = int.from_bytes(frame[:4], "big")
            if not 0 < size <= RANK_FRAME_BYTES - 4 or any(frame[4 + size :]):
                raise TrainingConflict("Rank measurement collective frame is malformed")
            ranks.append(TrainingMeasurementRankCheckpoint.model_validate_json(frame[4 : 4 + size]))
        pair = TrainingMeasurementRankSet(ranks=sorted(ranks, key=lambda r: r.rank))
        self.collective.compare(pair.canonical_sha256())
        return pair


class RankMeasurementCheckpoint:
    """Serialize actual local full state at a common update; never fabricate a bundle.

    The shared trainer's measurement callback owns the boundary. The independent
    guardian bounds a stalled all-gather even while short execution leases renew.
    """

    def __init__(
        self,
        probe: TrainingMeasurementPrepare,
        store: CheckpointStore,
        exchange: MeasurementRankExchange,
        guardian: DeadlineGuardian,
        *,
        control: Callable[[], str],
        sample: Callable[[], int],
        serializing: threading.Event,
    ) -> None:
        self.probe, self.store, self.exchange, self.guardian = probe, store, exchange, guardian
        self.control, self.sample, self.serializing = control, sample, serializing
        self.largest: TrainingMeasurementRankSet | None = None
        self.optimizer_bytes = 0

    def authorized(self) -> None:
        if self.guardian.expired() or self.control() in {"kill", "cancel"}:
            raise TrainingConflict("Rank measurement boundary authority or deadline ended")
        self.sample()

    def checkpoint(
        self, state: CheckpointWorkerState, adapter: dict[str, Any], optimizer: Any
    ) -> None:
        prepared = self.probe.prepare
        if (
            state.world_size != 2
            or any(
                getattr(state, k) != getattr(prepared, k)
                for k in ("job_id", "attempt_id", "fence", "rank", "world_size")
            )
            or state.runtime_sha256 != prepared.resolved.runtime_sha256
            or state.resolved_spec_sha256 != resolved_digest(prepared)
            or state.optimizer != prepared.resolved.spec.optim
        ):
            raise TrainingConflict("Rank measurement state differs from pinned execution")
        self.authorized()
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"coire:measurement:{self.probe.measurement_id}:{state.attempt_id}:{state.fence}:{state.completed_update}",
        )
        self.serializing.set()
        try:
            self.sample()
            with tracer.start_as_current_span("coire.node.training.measurement.serialize_rank"):
                component = self.store.save_rank(state, adapter, optimizer, artifact_id=identity)
                manifest = descriptor(identity, component)
                verify_directory(component.directory, manifest)
                encoded = manifest.model_dump_json().encode()
                write_atomic(component.directory / "component.json", encoded)
                _fsync_directory(component.directory)
                self.sample()
        finally:
            self.serializing.clear()
        self.authorized()
        local = TrainingMeasurementRankCheckpoint(
            measurement_id=self.probe.measurement_id,
            job_id=state.job_id,
            attempt_id=state.attempt_id,
            fence=state.fence,
            rank=0 if state.rank == 0 else 1,
            node=prepared.node,
            update=state.completed_update,
            runtime_sha256=state.runtime_sha256,
            resolved_spec_sha256=state.resolved_spec_sha256,
            component_sha256=manifest.canonical_sha256(),
            serialized_bytes=sum(f.bytes for f in component.files) + len(encoded),
            optimizer_bytes=tensor_bytes(optimizer),
        )
        self.optimizer_bytes = max(self.optimizer_bytes, local.optimizer_bytes)
        pair = self.exchange.exchange(local)
        # Revalidate injected transports and retain our real file hashes, never a replacement.
        pair = TrainingMeasurementRankSet.model_validate(pair.model_dump(mode="json"))
        if next(r for r in pair.ranks if r.rank == state.rank) != local:
            raise TrainingConflict("Peer exchange substituted the local measured rank component")
        if pair.serialized_bytes > prepared.resolved.resource_envelope.checkpoint_bytes:
            raise TrainingConflict(
                "Both serialized rank states exceed the full common disk envelope"
            )
        self.authorized()
        self.guardian.progress()
        if self.largest is None or pair.serialized_bytes > self.largest.serialized_bytes:
            self.largest = pair
        shutil.rmtree(component.directory.parent)
        _fsync_directory(self.store.root)


def tensor_bytes(value: Any) -> int:
    if hasattr(value, "nbytes"):
        return int(value.nbytes)
    if isinstance(value, dict):
        return sum(tensor_bytes(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return sum(tensor_bytes(v) for v in value)
    return 0


def python_buffer_bytes(value: Any, seen: set[int] | None = None) -> int:
    """Inspect allocated CPU input objects; never infer token-cache bytes from token counts."""
    visited = seen if seen is not None else set()
    if id(value) in visited:
        return 0
    visited.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, BaseModel):
        size += python_buffer_bytes(value.__dict__, visited)
    elif isinstance(value, dict):
        size += sum(
            python_buffer_bytes(k, visited) + python_buffer_bytes(v, visited)
            for k, v in value.items()
        )
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(python_buffer_bytes(v, visited) for v in value)
    elif is_dataclass(value) and not isinstance(value, type):
        size += sum(python_buffer_bytes(getattr(value, f.name), visited) for f in fields(value))
    elif hasattr(value, "__dict__"):
        size += python_buffer_bytes(vars(value), visited)
    return size


def probe_inputs(
    prepared: TrainingPrepareRequest, directory: Path, journal: TrainingJournal
) -> list[FrozenInputs]:
    """Every train and independently selected held-out source is a frozen input."""
    return load_all_frozen_inputs(prepared, directory, journal)


class MeasurementSupervisor(TrainingSupervisor):
    """Uses existing fenced journal, private source delivery and process ownership.

    The journal must share the engine admission lock. Its committed_bytes and
    held_disk_bytes must participate in every node admission/health calculation.
    accelerator_guard is a mandatory node-owned inventory/active-lease check.
    """

    def __init__(
        self,
        journal: TrainingJournal,
        *,
        interpreter: Path,
        accelerator_guard: Callable[[TrainingMeasurementPrepare], None],
        hardware_sha256: Callable[[], str],
        store_root: Path,
        artifact_root: Path,
        memory_available: Callable[[], int],
        disk_available: Callable[[], int],
        jaccl_hostfile: Path | None = None,
        jaccl_coordinator_port: int = 32323,
        otlp_endpoint: str | None = None,
    ) -> None:
        super().__init__(
            journal,
            interpreter=interpreter,
            store_root=store_root,
            artifact_root=artifact_root,
            memory_available=memory_available,
            disk_available=disk_available,
            jaccl_hostfile=jaccl_hostfile or Path(get_settings().sharding_jaccl_hostfile),
            jaccl_coordinator_port=jaccl_coordinator_port,
            otlp_endpoint=otlp_endpoint,
        )
        self.accelerator_guard = accelerator_guard
        self.hardware_sha256 = hardware_sha256

    def argv(self, value: dict[str, Any]) -> list[str]:
        argv = super().argv(value)
        argv[2] = "coire_node.training.measurement"
        argv[-1] = str(self.artifact_root / value["attempt_id"])
        return argv

    def probe(self, attempt: str) -> TrainingMeasurementPrepare:
        return TrainingMeasurementPrepare.model_validate_json(
            read_private(self.journal.root / f"measurement-{attempt}.json", 1024**2)
        )

    def rejected_probe(self, attempt: str) -> TrainingMeasurementPrepare | None:
        """A private immutable rejection fences a pristine namespace permanently."""
        TypeAdapter(TrainingId).validate_python(attempt)
        with self.journal.lock:
            path = self.journal.root / f"rejected-measurement-{attempt}.json"
            if not path.exists() and not path.is_symlink():
                return None
            rejected = TrainingMeasurementPrepare.model_validate_json(read_private(path, 1024**2))
            if (
                rejected.prepare.attempt_id != attempt
                or rejected.prepare.node != self.journal.node
                or any(r["attempt_id"] == attempt for r in self.journal.records())
                or (self.journal.root / attempt).exists()
                or (self.journal.root / attempt).is_symlink()
                or (self.artifact_root / attempt).exists()
                or (self.artifact_root / attempt).is_symlink()
                or (self.journal.root / f"measurement-{attempt}.json").exists()
                or (self.journal.root / f"measurement-{attempt}.json").is_symlink()
                or not attempt_process_absent(attempt)
            ):
                raise TrainingConflict("Rejected measurement ownership is unresolved")
            return rejected

    def release_stopped_attempt(self, attempt: str) -> None:
        with self.journal.lock:
            if self.rejected_probe(attempt) is None:
                super().release_stopped_attempt(attempt)

    def capabilities(self) -> TrainingMeasurementCapabilities:
        hook = measurement_hook_available()
        native = (
            platform.node().lower().split(".", 1)[0] == self.journal.node
            and platform.system() == "Darwin"
            and platform.machine() == "arm64"
        )
        worlds: list[Literal[1, 2]] = [1] if native else []
        if native and hook and self.jaccl_hostfile is not None:
            try:
                with self.jaccl_hostfile.open("rb") as source:
                    payload = source.read(64 * 1024 + 1)
                JacclLaunch(
                    self.jaccl_hostfile,
                    hashlib.sha256(payload).hexdigest(),
                    self.jaccl_coordinator_port,
                ).devices()
                worlds.append(2)
            except (OSError, ValueError):
                pass
        return TrainingMeasurementCapabilities(
            node=self.journal.node,
            hardware_sha256=self.hardware_sha256(),
            world_sizes=worlds,
            measurement_checkpoint=hook,
        )

    async def prepare_measurement(self, command: TrainingMeasurementPrepare) -> TrainingPrepared:
        command = TrainingMeasurementPrepare.model_validate(command.model_dump(mode="json"))
        if command.prepare.node != self.journal.node:
            raise TrainingConflict("Measurement preparation targets another Studio")
        if platform.node().lower().split(".", 1)[0] == "coire-core":
            raise TrainingValidationError("Measurement preparation is forbidden on core")
        if command.deadline > datetime.now(UTC) + timedelta(hours=1):
            raise TrainingValidationError("Probe deadline exceeds one hour")
        if command.prepare.world_size == 2 and not measurement_hook_available():
            raise TrainingValidationError(
                "Two-rank measurement needs the trainer's non-durable measurement hook"
            )
        with self.journal.lock:
            rejected = self.rejected_probe(command.prepare.attempt_id)
            if rejected is not None:
                if rejected != command:
                    raise TrainingConflict("Rejected probe preparation scope changed")
                raise TrainingConflict("Probe preparation was durably rejected")
            try:
                now = datetime.now(UTC)
                if (
                    not 0 < (command.prepare.lease_expires_at - now).total_seconds() <= 30
                    or command.deadline <= now
                ):
                    raise TrainingConflict("Probe preparation authority expired or is unbounded")
                if self.hardware_sha256() != command.hardware_sha256:
                    raise TrainingConflict("Probe hardware differs from current local inventory")
                self.accelerator_guard(command)
            except TrainingConflict:
                # Only a pristine, never-prepared namespace may attest no start.
                # Existing or unreadable ownership remains on the ordinary stop lane.
                if (
                    not any(
                        r["attempt_id"] == command.prepare.attempt_id
                        for r in self.journal.records()
                    )
                    and not (self.journal.root / command.prepare.attempt_id).exists()
                    and not (self.journal.root / command.prepare.attempt_id).is_symlink()
                    and not (self.artifact_root / command.prepare.attempt_id).exists()
                    and not (self.artifact_root / command.prepare.attempt_id).is_symlink()
                    and not (
                        self.journal.root / f"measurement-{command.prepare.attempt_id}.json"
                    ).exists()
                    and not (
                        self.journal.root / f"measurement-{command.prepare.attempt_id}.json"
                    ).is_symlink()
                    and attempt_process_absent(command.prepare.attempt_id)
                ):
                    with tracer.start_as_current_span(
                        "coire.node.training.measurement.reject"
                    ) as span:
                        span.set_attribute("coire.attempt_id", command.prepare.attempt_id)
                        span.set_attribute("coire.job_id", command.prepare.job_id)
                        write_atomic(
                            self.journal.root
                            / f"rejected-measurement-{command.prepare.attempt_id}.json",
                            command.model_dump_json().encode(),
                        )
                        _fsync_directory(self.journal.root)
                        outcomes.add(1, {"state": "rejected", "mode": command.mode})
                        logger.info(
                            "measurement preparation fenced without start",
                            extra={
                                "job_id": command.prepare.job_id,
                                "attempt_id": command.prepare.attempt_id,
                                "measurement_id": str(command.measurement_id),
                                "node": command.prepare.node,
                            },
                        )
                raise
            config_path = self.journal.root / f"measurement-{command.prepare.attempt_id}.json"
            if config_path.exists():
                if self.probe(command.prepare.attempt_id) != command:
                    raise TrainingConflict("Probe configuration is immutable")
            else:
                write_atomic(config_path, command.model_dump_json().encode())
                _fsync_directory(config_path.parent)
            # Acquire the full ceiling before any tokenizer/Metal preparation.
            self.journal.prepare(
                command.prepare,
                memory_available=self.memory_available() if self.memory_available else 0,
                disk_available=self.disk_available() if self.disk_available else 0,
                disk_floor=self.disk_floor,
            )
            path = self.directory(command.prepare.attempt_id) / "measurement.json"
            if path.exists():
                if self.probe(command.prepare.attempt_id) != command:
                    raise TrainingConflict("Probe configuration is immutable")
            else:
                write_atomic(path, command.model_dump_json().encode())
                _fsync_directory(path.parent)
        return await super().prepare(command.prepare)

    def validate_native_inputs(self, command: TrainingPrepareRequest) -> None:
        if platform.node().lower().split(".", 1)[0] == "coire-core":
            raise TrainingValidationError("Measurement tokenization is forbidden on core")
        super().validate_native_inputs(command)

    async def start(self, command: TrainingStartRequest) -> TrainingStartReceipt:
        with self.journal.lock:
            if self.rejected_probe(command.attempt_id) is not None:
                raise TrainingConflict("Rejected measurement cannot start")
            probe = self.probe(command.attempt_id)
            if self.hardware_sha256() != probe.hardware_sha256:
                raise TrainingConflict("Probe hardware changed before Metal launch")
            if probe.deadline <= datetime.now(UTC):
                raise TrainingConflict("Probe deadline expired")
            self.accelerator_guard(probe)
        return await super().start(command)

    def measurement_status(self, attempt: str) -> TrainingMeasurementNodeStatus:
        with self.journal.lock:
            rejected = self.rejected_probe(attempt)
            if rejected is not None:
                prepared = rejected.prepare
                return TrainingMeasurementNodeStatus(
                    measurement_id=rejected.measurement_id,
                    status=NodeTrainingStatus(
                        attempt_id=prepared.attempt_id,
                        job_id=prepared.job_id,
                        fence=prepared.fence,
                        node=prepared.node,
                        liveness="stopped",
                        update=0,
                        lease_expires_at=prepared.lease_expires_at,
                    ),
                    stopped=True,
                )
        status = self.observe(attempt)
        probe = self.probe(attempt)
        observation = None
        path = self.directory(attempt) / "observation.json"
        if path.exists() and status.liveness == "stopped":
            encoded = read_private(path, 1024**2)
            candidate = TrainingMeasurementObservation.model_validate_json(encoded)
            if (
                candidate.measurement_id != probe.measurement_id
                or candidate.attempt_id != attempt
                or candidate.node != probe.prepare.node
                or candidate.rank != probe.prepare.rank
                or candidate.world_size != probe.prepare.world_size
                or candidate.hardware_sha256 != probe.hardware_sha256
                or candidate.request_sha256 != payload_sha256(probe)
                or candidate.peak_footprint_bytes
                > probe.prepare.resolved.resource_envelope.memory_bytes
                or candidate.swap_growth_bytes
                or not candidate.thermal_ok
            ):
                raise TrainingConflict("Probe observation differs from reserved authority")
            digest = hashlib.sha256(encoded).hexdigest()
            with self.journal.transaction():
                value = self.journal.get(attempt)
                if value.get("measurement_result_sha256", digest) != digest:
                    raise TrainingConflict("Probe result changed after observation")
                first_observation = "measurement_result_sha256" not in value
                value["measurement_result_sha256"] = digest
                self.journal.save(value)
            if first_observation:
                outcomes.add(1, {"node": probe.prepare.node, "state": "observed"})
                logger.info(
                    "native measurement evidence observed",
                    extra={
                        "job_id": probe.prepare.job_id,
                        "attempt_id": attempt,
                        "model_id": str(probe.prepare.resolved.spec.model.model_id),
                        "measurement_id": str(probe.measurement_id),
                        "node": probe.prepare.node,
                        "report_sha256": digest,
                        "peak_footprint_bytes": candidate.peak_footprint_bytes,
                    },
                )
            observation = candidate
        return TrainingMeasurementNodeStatus(
            measurement_id=probe.measurement_id,
            status=status,
            observation=observation,
            stopped=status.liveness == "stopped",
            ready=status.liveness == "running" and (path.parent / "measurement-ready").exists(),
            training_started_at=datetime.fromisoformat(
                read_private(path.parent / "training-started", 128).decode()
            )
            if (path.parent / "training-started").exists()
            else None,
            swap_used_bytes=psutil.swap_memory().used,
            swap_out_bytes=psutil.swap_memory().sout,
            thermal_state=read_thermal_state().value,
            sampled_at=datetime.now(UTC),
        )

    def begin_mixed(self, attempt: str) -> None:
        probe = self.probe(attempt)
        if (
            probe.mode != "coexistence" and probe.prepare.world_size != 2
        ) or not self.measurement_status(attempt).ready:
            raise TrainingConflict("Mixed phase needs a ready owned trainer")
        write_atomic(self.directory(attempt) / "mixed-go", b"1")

    async def stop(self, command: TrainingStopRequest) -> TrainingStopReceipt:
        command = TrainingStopRequest.model_validate(command.model_dump(mode="json"))
        with self.journal.lock:
            rejected = self.rejected_probe(command.attempt_id)
            if rejected is not None:
                if any(
                    getattr(command, field) != getattr(rejected.prepare, field)
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
                    raise TrainingConflict("Rejected probe stop scope changed")
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
        receipt = await super().stop(command)
        if receipt.stopped:
            # Probe tensors and raw data never become a retained training artifact.
            for path in (
                self.artifact_root / command.attempt_id,
                self.directory(command.attempt_id) / "scratch",
            ):
                if path.is_symlink():
                    raise TrainingConflict("Probe cleanup encountered a link")
                if path.exists():
                    shutil.rmtree(path)
            directory = self.directory(command.attempt_id)
            for name in ("source.jsonl", "binding.json", "split.json", "analysis.json"):
                (directory / name).unlink(missing_ok=True)
            for selected in self.probe(command.attempt_id).prepare.resolved.datasets:
                source_directory = directory / str(selected.dataset_id)
                if source_directory.is_symlink():
                    raise TrainingConflict("Probe source cleanup encountered a link")
                if source_directory.exists():
                    shutil.rmtree(source_directory)
            _fsync_directory(directory)
            with self.journal.transaction():
                value = self.journal.get(command.attempt_id)
                value["disk_bytes"] = 0
                self.journal.save(value)
        return receipt

    async def watchdog(self) -> None:
        # Prepared probes await a real baseline, not a trainer execution lease.
        # Spawned probes retain the usual short lease; deadline bounds preparation.
        import asyncio

        while True:
            for value in await asyncio.to_thread(self.journal.records):
                if value["released"]:
                    continue
                probe = await asyncio.to_thread(self.probe, value["attempt_id"])
                status = await asyncio.to_thread(self.observe, value["attempt_id"])
                expired = probe.deadline <= datetime.now(UTC) or (
                    value["spawn_nonce"] is not None
                    and status.lease_expires_at <= datetime.now(UTC)
                )
                if expired or status.liveness == "stopped":
                    receipt = await self.stop(
                        TrainingStopRequest(
                            **{
                                k: getattr(probe.prepare, k)
                                for k in (
                                    "job_id",
                                    "attempt_id",
                                    "fence",
                                    "request_sha256",
                                    "node",
                                    "rank",
                                    "world_size",
                                )
                            },
                            command_id=uuid.uuid4(),
                            lease_expires_at=datetime.now(UTC),
                            reason="lease_expired",
                        )
                    )
                    if receipt.stopped:
                        await asyncio.to_thread(
                            self.journal.release_after_death, value["attempt_id"]
                        )
            await asyncio.sleep(0.5)


def native_measurement(
    journal: TrainingJournal,
    prepared: TrainingPrepareRequest,
    *,
    owner: str,
    store_root: Path,
    artifact_root: Path,
) -> int:
    if (
        platform.node().lower().split(".", 1)[0] not in {"coire-edge-a", "coire-edge-b"}
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
    ):
        raise TrainingValidationError("Measurement executes only on declared native Studios")
    if prepared.world_size == 2 and not measurement_hook_available():
        raise TrainingValidationError(
            "Two-rank measurement callback is unavailable before model loading"
        )
    directory = journal.root / prepared.attempt_id
    probe = TrainingMeasurementPrepare.model_validate_json(
        read_private(journal.root / f"measurement-{prepared.attempt_id}.json", 1024**2)
    )
    record = journal.get(prepared.attempt_id)
    if (
        probe.prepare != prepared
        or record["spawn_nonce"] != owner
        or record["released"]
        or command_digest(TrainingPrepareRequest.model_validate(record["prepare"]))
        != command_digest(prepared)
        or os.getpgrp() != os.getpid()
        or prepared.resolved.worker_version != "1"
    ):
        raise TrainingConflict("Measurement does not own the pinned spawn intent")
    channel = PrivateControl(directory, prepared, owner)
    birth = TrainingStartReceipt(
        attempt_id=prepared.attempt_id,
        fence=prepared.fence,
        pid=os.getpid(),
        process_create_time=psutil.Process().create_time(),
        reservation_id=prepared.reservation_id,
    )
    write_atomic(directory / "birth.json", birth.model_dump_json().encode())
    _fsync_directory(directory)
    started, stop = time.monotonic(), threading.Event()
    swap_start = psutil.swap_memory()
    peak, swap_growth, samples, checkpoint_peak = 0, 0, 0, 0
    serializing = threading.Event()
    guardian: DeadlineGuardian | None = None
    thermal_ok = True
    lock = threading.Lock()

    def sample() -> int:
        nonlocal peak, swap_growth, samples, thermal_ok, checkpoint_peak
        with lock:
            footprint = phys_footprint(os.getpid())
            swap = psutil.swap_memory()
            thermal = read_thermal_state()
            if footprint is None or thermal not in {"nominal", "fair"}:
                thermal_ok = False
                raise TrainingValidationError("Probe resource telemetry missing or unsafe")
            peak = max(peak, footprint)
            if serializing.is_set():
                checkpoint_peak = max(checkpoint_peak, footprint)
            swap_growth = max(swap_growth, swap.used - swap_start.used, swap.sout - swap_start.sout)
            samples += 1
            if (
                swap_growth
                or footprint > prepared.resolved.resource_envelope.memory_bytes
                or datetime.now(UTC) >= probe.deadline
                or channel.poll() in {"kill", "cancel"}
                or (guardian is not None and guardian.expired())
            ):
                raise TrainingConflict("Probe guard ended execution")
            return footprint

    sample()  # Mandatory safety observation before any model/tokenizer work.

    def watchdog() -> None:
        while not stop.wait(0.5):
            try:
                sample()
            except Exception:
                os._exit(124)

    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    try:
        all_inputs = probe_inputs(prepared, directory, journal)
        inputs = all_inputs[0]
        model_path = Store(store_root).path_for(inputs.binding.model_slug)
        tokenizer, tok, template, runtime_sha = load_analysis_tokenizer(model_path, inputs.binding)
        if (tok, template, runtime_sha) != (
            prepared.resolved.tokenizer_sha256,
            prepared.resolved.template_sha256,
            prepared.resolved.runtime_sha256,
        ):
            raise TrainingConflict("Probe runtime differs from frozen analysis")
        compiler_buffer_bytes = 0

        def observe_buffer(amount: int) -> None:
            nonlocal compiler_buffer_bytes
            compiler_buffer_bytes = max(compiler_buffer_bytes, amount)

        training, validation = compile_samples(
            prepared, all_inputs, tokenizer, buffer_observer=observe_buffer
        )
        sample()
        source = validate_sft_input(
            model_path,
            prepared.resolved.spec.parameterization,
            expected_manifest_sha256=prepared.resolved.base_manifest_sha256,
        )
        with tracer.start_as_current_span("coire.node.training.load"):
            runtime = load_sft_runtime(source, seed=prepared.resolved.spec.seed)
        optimizer = make_optimizer(prepared.resolved.spec.optim)
        import mlx.core as mx

        params = runtime.parameters()
        mx.eval(params)
        weights = sum(int(v.nbytes) for k, v in params.items() if k not in runtime.trainable_keys)
        adapter = sum(int(params[k].nbytes) for k in runtime.trainable_keys)
        checkpoint_bytes, optimizer_bytes = 0, 0

        class ObservedCheckpointStore(CheckpointStore):
            def save(
                self,
                state: CheckpointWorkerState,
                adapter: dict[str, Any],
                optimizer_state: Any,
                *,
                artifact_id: uuid.UUID | None = None,
            ) -> TrainingArtifactManifest:
                # Instrument the existing serializer; do not change tensor formats or trainer calls.
                serializing.set()
                sample()
                try:
                    with tracer.start_as_current_span("coire.node.training.measurement.serialize"):
                        return super().save(
                            state, adapter, optimizer_state, artifact_id=artifact_id
                        )
                finally:
                    sample()
                    serializing.clear()

        checkpoints = ObservedCheckpointStore(
            artifact_root, max_bytes=prepared.resolved.resource_envelope.checkpoint_bytes
        )
        write_atomic(directory / "measurement-ready", b"1")
        _fsync_directory(directory)
        if probe.mode == "coexistence" or prepared.world_size == 2:
            while not (directory / "mixed-go").exists():
                sample()
                time.sleep(0.1)
        collective = None
        rank_checkpoint = None
        if prepared.world_size == 2:
            if not measurement_hook_available() or prepared.collective is None:
                raise TrainingValidationError(
                    "Rank probe needs its bound collective and measurement callback"
                )
            guardian = DeadlineGuardian()
            collective = BareCollective(prepared)
            rank_checkpoint = RankMeasurementCheckpoint(
                probe,
                checkpoints,
                BareMeasurementRankExchange(collective),
                guardian,
                control=channel.poll,
                sample=sample,
                serializing=serializing,
            )
        training_started = datetime.now(UTC)
        write_atomic(directory / "training-started", training_started.isoformat().encode())

        def local_probe_save(manifest: TrainingArtifactManifest) -> CheckpointCommitAcknowledgement:
            # This receipt lets the unchanged trainer proceed in an isolated experiment.
            # It is never sent to core or published as a durable training checkpoint.
            nonlocal checkpoint_bytes, optimizer_bytes
            checkpoint_bytes = max(
                checkpoint_bytes, manifest.total_bytes + len(manifest.model_dump_json().encode())
            )
            mx.eval(optimizer.state)
            optimizer_bytes = max(optimizer_bytes, tensor_bytes(optimizer.state))
            # The complete serialized bundle has been measured. No probe checkpoint
            # is eligible for publication or recovery; free its bounded scratch.
            shutil.rmtree(artifact_root / str(manifest.artifact_id))
            _fsync_directory(artifact_root)
            return CheckpointCommitAcknowledgement(
                **{
                    k: getattr(prepared, k)
                    for k in (
                        "job_id",
                        "attempt_id",
                        "fence",
                        "request_sha256",
                        "node",
                        "rank",
                        "world_size",
                    )
                },
                command_id=uuid.uuid4(),
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                checkpoint_id=manifest.artifact_id,
                manifest_sha256=manifest.canonical_sha256(),
                update=manifest.update or 0,
            )

        rank_arguments: dict[str, Any] = {}
        if rank_checkpoint is not None:
            rank_arguments = {
                "measurement_checkpoint": rank_checkpoint.checkpoint,
                "collective": collective,
                "guardian": guardian,
            }
        completed = run_sft(
            prepared,
            runtime,
            optimizer,
            training,
            validation,
            store=checkpoints,
            scratch=directory / "scratch",
            emit=journal.append_event,
            commit=local_probe_save,
            control=channel.poll,
            footprint=sample,
            **rank_arguments,
        )
        training_finished = datetime.now(UTC)
        sample()
        buffers = max(
            python_buffer_bytes((vars(training), vars(validation))), compiler_buffer_bytes
        )
        rank_summaries = []
        if rank_checkpoint is not None:
            if rank_checkpoint.largest is None:
                raise TrainingConflict("Rank probe did not serialize any common evaluated state")
            checkpoint_bytes = rank_checkpoint.largest.serialized_bytes
            optimizer_bytes = rank_checkpoint.optimizer_bytes
            rank_summaries = rank_checkpoint.largest.ranks
        observation = TrainingMeasurementObservation(
            measurement_id=probe.measurement_id,
            attempt_id=prepared.attempt_id,
            node=prepared.node,
            hardware_sha256=probe.hardware_sha256,
            request_sha256=payload_sha256(probe),
            completed_updates=completed,
            elapsed_seconds=time.monotonic() - started,
            training_started_at=training_started,
            training_finished_at=training_finished,
            peak_footprint_bytes=peak,
            peak_mlx_bytes=int(mx.get_peak_memory()),
            weight_bytes=weights,
            adapter_bytes=adapter,
            optimizer_bytes=optimizer_bytes,
            buffer_bytes=buffers,
            checkpoint_bytes=checkpoint_bytes,
            serialization_peak_bytes=checkpoint_peak,
            swap_growth_bytes=swap_growth,
            thermal_ok=thermal_ok,
            sample_count=samples,
            measured_at=datetime.now(UTC),
            rank=0 if prepared.rank == 0 else 1,
            world_size=prepared.world_size,
            rank_checkpoints=rank_summaries,
        )
        path = directory / "observation.json"
        if path.exists():
            raise TrainingConflict("Probe result is immutable")
        write_atomic(path, observation.model_dump_json().encode())
        _fsync_directory(directory)
        outcomes.add(1, {"node": prepared.node, "state": "succeeded"})
        return 0
    finally:
        stop.set()
        watcher.join(timeout=4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", required=True, type=TypeAdapter(TrainingId).validate_python)
    parser.add_argument("--owner", required=True, type=uuid.UUID)
    for name in ("state-root", "store-root", "artifact-root"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    if not all(p.is_absolute() for p in (args.state_root, args.store_root, args.artifact_root)):
        parser.error("probe roots must be absolute node configuration")
    prepared = TrainingPrepareRequest.model_validate_json(
        read_private(args.state_root / args.attempt / "prepare.json", 1024**2)
    )
    if prepared.attempt_id != args.attempt:
        parser.error("probe attempt differs from owned intent")
    from coire_node.training.telemetry import initialize_training_telemetry

    initialize_training_telemetry()
    journal = TrainingJournal(args.state_root, node=prepared.node, admission_lock=threading.RLock())
    try:
        with tracer.start_as_current_span(
            "coire.node.training.measurement.execute",
            attributes={
                "job_id": prepared.job_id,
                "attempt_id": prepared.attempt_id,
                "node": prepared.node,
            },
        ):
            return native_measurement(
                journal,
                prepared,
                owner=str(args.owner),
                store_root=args.store_root,
                artifact_root=args.artifact_root,
            )
    finally:
        journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
