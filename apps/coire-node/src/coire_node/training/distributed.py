"""Bare JACCL rank attachment and full-bundle coordination, without a trainer fork.

Transport callbacks use strict shared component events and authenticated collection
mailboxes. Full-bundle staging precedes controller mirror verification and fenced commit.
There is deliberately no local-success durability implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentelemetry import metrics, trace

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    CheckpointWorkerState,
    NodeRankCheckpointPayload,
    NodeTrainingEvent,
    TrainingArtifactManifest,
    TrainingPrepareRequest,
    TrainingRankCollection,
)
from coire_node.store import write_atomic
from coire_node.training.checkpoints import CheckpointStore, RankCheckpointComponent, TensorIO
from coire_node.training.components import TrainingComponents
from coire_node.training.journal import TrainingJournal

NODES = ("coire-edge-a", "coire-edge-b")
tracer = trace.get_tracer("coire.node.training")
logger = logging.getLogger(__name__)
outcomes = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_rank_coordination_total"
)


@dataclass(frozen=True)
class JacclLaunch:
    """Node configuration + controller-selected hostfile identity; never caller hosts.

    Match mlx 0.32.2 launch_jaccl's public runtime environment, while each node
    starts only its own process group through authenticated prepare/start dispatch.
    """

    hostfile: Path
    hostfile_sha256: str
    coordinator_port: int

    def devices(self) -> list[list[str | list[str] | None]]:
        if not self.hostfile.is_absolute() or self.hostfile.is_symlink():
            raise TrainingValidationError("JACCL requires a declared generated hostfile")
        encoded = self.hostfile.read_bytes()
        if len(encoded) > 64 * 1024 or hashlib.sha256(encoded).hexdigest() != self.hostfile_sha256:
            raise TrainingConflict("JACCL hostfile differs from frozen launch identity")
        if not 1 <= self.coordinator_port <= 65535:
            raise TrainingValidationError("JACCL coordinator port is invalid")
        try:
            value = json.loads(encoded)
            hosts = value["hosts"]
            if value["backend"] != "jaccl" or len(hosts) != 2:
                raise ValueError
            if [h["ssh"] for h in hosts] != [f"{node}.fabric" for node in NODES]:
                raise ValueError
            # Hostfile env cannot introduce code, credentials or override rank ownership.
            envs = value.get("envs", [])
            if not isinstance(envs, list) or any(e != "MLX_METAL_FAST_SYNCH=1" for e in envs):
                raise ValueError
            devices = [h["rdma"] for h in hosts]
            for rank, row in enumerate(devices):
                if len(row) != 2 or row[rank] is not None:
                    raise ValueError
                peer = row[1 - rank]
                names = peer if isinstance(peer, list) else [peer]
                if not 1 <= len(names) <= 8 or any(
                    not isinstance(name, str) or not name or len(name) > 128 for name in names
                ):
                    raise ValueError
            return [[row[0], row[1]] for row in devices]
        except (KeyError, TypeError, ValueError):
            raise TrainingValidationError(
                "Generated JACCL inventory is not the declared pair"
            ) from None

    def environment(self, prepared: TrainingPrepareRequest, directory: Path) -> dict[str, str]:
        if prepared.world_size != 2 or prepared.node != NODES[prepared.rank]:
            raise TrainingConflict("JACCL launch differs from declared rank scope")
        devices = self.devices()
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise TrainingValidationError("JACCL rank runtime directory must be private")
        path = directory / "jaccl-devices.json"
        write_atomic(path, json.dumps(devices, separators=(",", ":")).encode())
        return {
            "MLX_RANK": str(prepared.rank),
            "MLX_IBV_DEVICES": str(path),
            "MLX_JACCL_COORDINATOR": f"{NODES[0]}.fabric:{self.coordinator_port}",
            "MLX_METAL_FAST_SYNCH": "1",
        }


class Collective(Protocol):
    def compare(self, digest: str) -> None: ...
    def action(self, local: str) -> str: ...


class BareCollective:
    """Use MLX's global group: unchanged train/evaluate initialize that same group."""

    def __init__(self, prepared: TrainingPrepareRequest) -> None:
        from coire_node.training.checkpoints import MlxTensorIO

        self.mx = MlxTensorIO.module()
        self.group = self.mx.distributed.init(strict=True, backend="jaccl")
        if self.group.size() != prepared.world_size or self.group.rank() != prepared.rank:
            raise TrainingConflict("Bare collective differs from owned rank identity")

    def compare(self, digest: str) -> None:
        mx = self.mx
        local = mx.array(list(bytes.fromhex(digest)), dtype=mx.uint8)
        gathered = mx.distributed.all_gather(local, group=self.group, stream=mx.cpu)
        mx.eval(gathered)
        values = gathered.tolist()
        if values != list(bytes.fromhex(digest)) * self.group.size():
            raise TrainingConflict("Ranks disagree on identical parameter/bundle identity")

    def action(self, local: str) -> str:
        if local not in {"continue", "pause", "kill", "cancel"}:
            raise TrainingConflict("Invalid rank execution authority")
        mx = self.mx
        flag = mx.array(2 if local in {"kill", "cancel"} else int(local == "pause"))
        result = mx.distributed.all_max(flag, group=self.group, stream=mx.cpu).item()
        return ("continue", "pause", "kill")[result]


def parameter_digest(parameters: dict[str, Any], codec: TensorIO) -> str:
    """Hash evaluated raw bytes plus exact names/shapes/dtypes, not tensor reprs.

    memoryview avoids conversion to float32 and preserves BF16/quantized bit patterns.
    Runs only on the Studio training thread for MLX; CPU codecs support simulation.
    """
    digest = hashlib.sha256()
    for name, tensor in sorted(parameters.items()):
        if not codec.is_tensor(tensor):
            raise TrainingValidationError("Parameter hash requires evaluated tensors")
        header = json.dumps(
            [name, codec.shape(tensor), codec.dtype(tensor)], separators=(",", ":")
        ).encode()
        digest.update(len(header).to_bytes(8, "big"))
        digest.update(header)
        raw = memoryview(tensor)
        if not raw.c_contiguous:
            raise TrainingValidationError("Parameter hash requires contiguous evaluated tensors")
        octets = raw.cast("B")
        digest.update(octets.nbytes.to_bytes(8, "big"))
        for offset in range(0, octets.nbytes, 1024 * 1024):
            digest.update(octets[offset : offset + 1024 * 1024])
    return digest.hexdigest()


class DeadlineGuardian:
    """Independent guardian: renewed leases cannot prolong a blocked collective forever.

    Native terminate must exit the owned process, not attempt another collective or
    wait for a stuck training thread. Supervisor supplies process-group stop proof.
    """

    def __init__(self, *, seconds: float = 60, clock: Callable[[], float] = time.monotonic) -> None:
        if not 0 < seconds <= 60:
            raise TrainingValidationError("Rank progress deadline must be finite and <=60 seconds")
        self.clock = clock
        self.seconds = seconds
        self.lock = threading.Lock()
        self.deadline = clock() + seconds

    def progress(self) -> None:
        with self.lock:
            if self.clock() >= self.deadline:
                raise TrainingConflict("Rank collective/progress deadline expired")
            self.deadline = self.clock() + self.seconds

    def expired(self) -> bool:
        with self.lock:
            return self.clock() >= self.deadline


class RankTransport(Protocol):
    """Authenticated, fenced component transport adapter; every operation is bounded.

    collect returns local private copies of BOTH components. The assembled common
    bundle is staging; the worker separately awaits controller both-copy commitment.
    Implementations must poll control/lease and honor the monotonic deadline even
    during grant refresh. They must never route tensor bytes through core.
    """

    def publish(
        self, artifact_id: uuid.UUID, component: RankCheckpointComponent, deadline: float
    ) -> None: ...
    def collect(
        self, artifact_id: uuid.UUID, state: CheckpointWorkerState, deadline: float
    ) -> Sequence[RankCheckpointComponent]: ...


class PrivateRankTransport:
    """Worker mailbox bridge: controller imports peers before delivering collection.

    Full bundles are emitted as staging by run_sft. Their fenced controller commit
    acknowledgement is the both-copy proof; never wait for mirroring before that event.
    """

    def __init__(
        self,
        prepared: TrainingPrepareRequest,
        components: TrainingComponents,
        journal: TrainingJournal,
        *,
        read: Callable[[str], bytes],
        control: Callable[[], str],
        scope: Callable[[Any], None],
    ) -> None:
        self.prepared, self.components, self.journal = prepared, components, journal
        self.read, self.control, self.scope = read, control, scope

    def publish(
        self, artifact_id: uuid.UUID, component: RankCheckpointComponent, deadline: float
    ) -> None:
        if time.monotonic() >= deadline or self.control() in {"cancel", "kill"}:
            raise TrainingConflict("Rank publication authority expired")
        manifest = self.components.register(artifact_id, component)
        from datetime import UTC, datetime

        self.journal.append_event(
            NodeTrainingEvent(
                sequence=self.journal.next_sequence(self.prepared.attempt_id),
                job_id=manifest.job_id,
                attempt_id=manifest.attempt_id,
                fence=manifest.fence,
                update=manifest.update,
                recorded_at=datetime.now(UTC),
                payload=NodeRankCheckpointPayload(component=manifest),
            )
        )

    def collect(
        self, artifact_id: uuid.UUID, state: CheckpointWorkerState, deadline: float
    ) -> Sequence[RankCheckpointComponent]:
        while time.monotonic() < deadline:
            if self.control() in {"kill", "cancel"}:
                raise TrainingConflict("Rank collection authority ended")
            try:
                command = TrainingRankCollection.model_validate_json(
                    self.read(f"collection-{artifact_id}.json")
                )
            except FileNotFoundError:
                time.sleep(0.1)
                continue
            self.scope(command)
            if command.artifact_id != artifact_id or any(
                c.update != state.completed_update for c in command.components
            ):
                raise TrainingConflict("Rank mailbox collection differs from requested update")
            return [self.components.verify(c) for c in command.components]
        raise TrainingConflict("Rank collection deadline expired")


class RankCheckpointCoordinator:
    def __init__(
        self,
        prepared: TrainingPrepareRequest,
        store: CheckpointStore,
        transport: RankTransport,
        collective: Collective,
        *,
        control: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.prepared, self.store, self.transport = prepared, store, transport
        self.collective, self.control, self.clock = collective, control, clock

    def authorized(self, deadline: float) -> None:
        if self.clock() >= deadline or self.control() in {"kill", "cancel"}:
            raise TrainingConflict("Rank checkpoint transport authority/deadline ended")

    def checkpoint(
        self, state: CheckpointWorkerState, adapter: dict[str, Any], optimizer: Any
    ) -> TrainingArtifactManifest:
        prepared = self.prepared
        if (
            any(
                getattr(state, key) != getattr(prepared, key)
                for key in ("job_id", "attempt_id", "fence", "rank", "world_size")
            )
            or state.world_size != 2
        ):
            raise TrainingConflict("Rank snapshot differs from owned attempt")
        # Deterministic attempt/update identity is identical on both ranks and cannot
        # collide with another fence or a recovered attempt.
        artifact_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"coire:checkpoint:{state.job_id}:{state.attempt_id}:{state.fence}:{state.completed_update}",
        )
        deadline = self.clock() + 60
        with tracer.start_as_current_span("coire.node.training.rank.checkpoint"):
            try:
                self.authorized(deadline)
                component = self.store.save_rank(state, adapter, optimizer, artifact_id=artifact_id)
                self.authorized(deadline)
                self.transport.publish(artifact_id, component, deadline)
                components = self.transport.collect(artifact_id, state, deadline)
                self.authorized(deadline)
                local = [c for c in components if c.state.rank == state.rank]
                if (
                    len(local) != 1
                    or local[0].state != component.state
                    or local[0].files != component.files
                    or local[0].rank_manifest != component.rank_manifest
                ):
                    raise TrainingConflict("Collector substituted the owned rank snapshot")
                if any(
                    any(
                        getattr(c.state, key) != getattr(state, key)
                        for key in (
                            "job_id",
                            "attempt_id",
                            "fence",
                            "completed_update",
                            "world_size",
                            "runtime_sha256",
                            "resolved_spec_sha256",
                            "optimizer",
                        )
                    )
                    for c in components
                ):
                    raise TrainingConflict("Collected rank differs from local checkpoint lineage")
                manifest = self.store.publish_rank_bundle(
                    sorted(components, key=lambda c: c.state.rank), artifact_id=artifact_id
                )
                if (
                    any(
                        getattr(manifest, key) != getattr(state, key)
                        for key in (
                            "job_id",
                            "attempt_id",
                            "fence",
                            "world_size",
                            "runtime_sha256",
                            "resolved_spec_sha256",
                        )
                    )
                    or manifest.update != state.completed_update
                ):
                    raise TrainingConflict(
                        "Collected common bundle differs from local rank lineage"
                    )
                self.collective.compare(manifest.canonical_sha256())
                self.authorized(deadline)
                outcomes.add(1, {"outcome": "staged", "node": prepared.node})
                return manifest
            except Exception:
                outcomes.add(1, {"outcome": "failed", "node": prepared.node})
                logger.warning(
                    "rank checkpoint coordination failed",
                    extra={
                        "job_id": state.job_id,
                        "attempt_id": state.attempt_id,
                        "update": state.completed_update,
                    },
                )
                raise
