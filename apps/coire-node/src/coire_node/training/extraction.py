"""Offline, header-only adapter extraction with durable intent and private publication.

No MLX/model imports or tensor materialization occur here. The controller still owns
authorization/fencing, mirroring, inference smoke, and registry readiness.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import Depends, FastAPI
from opentelemetry import metrics, trace
from safetensors import safe_open

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.acquisition import ReservationRequest, ReservationState
from coire_core.models.jobs import ChecksumManifest
from coire_core.models.training import TrainingReason
from coire_core.models.training_node import (
    CheckpointWorkerState,
    TrainingAdapterExtractionStatus,
    TrainingAdapterExtractRequest,
    TrainingArtifactFile,
    TrainingArtifactManifest,
)
from coire_core.settings import Settings
from coire_node.reservations import ReservationLedger, ReservationRefused
from coire_node.store import Store, write_atomic
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.checkpoints import _check_tensor_header, _fsync_directory
from coire_node.training.objectives import APPROVED_ARCHITECTURES, APPROVED_TARGETS

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.training.extraction")
outcomes = metrics.get_meter("coire.node.training.extraction").create_counter(
    "coire_training_adapter_extraction_outcomes_total"
)
MAX_METADATA = 64 * 1024**2
CHUNK = 1024 * 1024


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def private_read(path: Path, maximum: int) -> bytes:
    with private_open(path) as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            raise TrainingValidationError("Extraction metadata exceeds its bound")
        return stream.read(maximum + 1)


def private_open(path: Path) -> BinaryIO:
    if any(parent.is_symlink() for parent in path.parents):
        raise TrainingValidationError("Extraction path is linked")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        os.close(fd)
        raise TrainingValidationError("Extraction file is unsafe")
    return os.fdopen(fd, "rb")


class AdapterExtractor:
    """One in-process copy at a time; startup reconciles interrupted copies before serving.

    POST runs in a thread, so control/GET remain responsive. A process crash cannot
    leave a tensor worker alive: extraction creates no subprocesses.
    """

    def __init__(
        self,
        artifacts: TrainingArtifacts,
        reservations: ReservationLedger,
        settings: Settings,
        store: Store,
    ) -> None:
        self.artifacts = artifacts
        self.reservations = reservations
        self.settings = settings
        self.store = store
        self.root = Path(settings.node_state_dir) / "training" / "extractions"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise TrainingValidationError("Extraction journal root is linked")
        if self.root.stat().st_mode & 0o077:
            raise TrainingValidationError("Extraction journal root must be private")
        self.lock = threading.RLock()
        self.recover()

    def attach(self, control: FastAPI) -> None:
        from coire_node.routes.training_extraction import router

        control.state.training_adapter_extractor = self
        control.include_router(router, dependencies=[Depends(control.state.require_node_token)])

    def _path(self, identity: uuid.UUID) -> Path:
        return self.root / f"{identity}.json"

    def _read(self, identity: uuid.UUID) -> dict[str, Any]:
        record: dict[str, Any] = json.loads(private_read(self._path(identity), MAX_METADATA))
        request = TrainingAdapterExtractRequest.model_validate(record["request"])
        status = TrainingAdapterExtractionStatus.model_validate(record["status"])
        if (
            request.command_id != identity
            or status.command_id != identity
            or status.adapter_id != request.adapter_id
            or status.checkpoint_id != request.checkpoint_id
            or status.node != request.node
        ):
            raise TrainingValidationError("Extraction journal identity differs")
        return record

    def _save(self, record: dict[str, Any]) -> None:
        identity = uuid.UUID(record["request"]["command_id"])
        write_atomic(self._path(identity), canonical(record))
        _fsync_directory(self.root)

    def status(self, identity: uuid.UUID) -> TrainingAdapterExtractionStatus:
        return TrainingAdapterExtractionStatus.model_validate(self._read(identity)["status"])

    def _status(self, record: dict[str, Any], **changes: Any) -> None:
        status = TrainingAdapterExtractionStatus.model_validate({**record["status"], **changes})
        record["status"] = status.model_dump(mode="json")
        self._save(record)

    def _staging(self, command: TrainingAdapterExtractRequest) -> Path:
        return self.artifacts.root / f".extract-{command.command_id}"

    def _reservation(self, command: TrainingAdapterExtractRequest) -> ReservationRequest:
        return ReservationRequest(
            idempotency_key=command.disk_reservation_id,
            workflow_id=command.command_id,
            variant_id=command.resolved.spec.model.variant_id,
            memory_bytes=MAX_METADATA + 2 * CHUNK,
            disk_bytes=command.max_bytes,
        )

    def _hold(self, command: TrainingAdapterExtractRequest) -> None:
        # Persisted files count after a hold is released; other filesystem holds
        # count before their bytes exist. Never turn successful publication into
        # unaccounted quota merely by releasing its temporary copy reservation.
        if self.reservations.get(command.disk_reservation_id) is None:
            used = 0
            for path in self.artifacts.root.rglob("*"):
                self._deadline(command)
                if path.is_symlink():
                    raise TrainingValidationError("Artifact quota scan encountered a link")
                if path.is_file():
                    used += path.stat().st_size
            if (
                used + self.reservations.held_disk_bytes(self.artifacts.root) + command.max_bytes
                > self.settings.training_artifact_quota_bytes
            ):
                raise ReservationRefused(
                    impossible=False,
                    required=command.max_bytes,
                    committed=used,
                    budget=self.settings.training_artifact_quota_bytes,
                )
        held, _created = self.reservations.hold(
            self._reservation(command),
            disk_path=self.artifacts.root,
            disk_floor_bytes=self.settings.training_artifact_disk_floor_bytes,
            require_stop=True,
        )
        if held.state is not ReservationState.HELD:
            raise TrainingConflict("Extraction disk reservation is no longer held")
        self.reservations.bind_owner(
            held.id,
            release_check=lambda: (
                not self._staging(command).exists()
                and self.status(command.command_id).state in {"succeeded", "failed"}
            ),
            footprint_bytes=lambda: None,
        )

    def _release(self, command: TrainingAdapterExtractRequest) -> None:
        if not self.reservations.release(command.disk_reservation_id):
            raise TrainingValidationError("Extraction reservation needs reconciliation")

    def recover(self) -> None:
        """Fail interrupted staging, or acknowledge exactly journaled published bytes."""
        for path in self.root.glob("*.json"):
            record = self._read(uuid.UUID(path.stem))
            command = TrainingAdapterExtractRequest.model_validate(record["request"])
            held = self.reservations.get(command.disk_reservation_id)
            if held is not None and held.state is not ReservationState.RELEASED:
                self._hold(command)  # Validate ownership before binding/releasing any hold.
            staging = self._staging(command)
            if staging.is_symlink():
                raise TrainingValidationError("Extraction staging is linked")
            if staging.exists():
                shutil.rmtree(staging)
                _fsync_directory(self.artifacts.root)
            status = TrainingAdapterExtractionStatus.model_validate(record["status"])
            if status.state in {"queued", "running"}:
                expected = record.get("publish_manifest")
                try:
                    if expected is None:
                        raise ValueError("publication not prepared")
                    manifest = TrainingArtifactManifest.model_validate(expected)
                    self.artifacts.verify(command.adapter_id, manifest.canonical_sha256())
                    _fsync_directory(self.artifacts.root)
                    self._status(record, state="succeeded", manifest=expected, reason=None)
                except (OSError, ValueError):
                    self._status(record, state="failed", manifest=None, reason="internal")
            if held is not None and held.state is not ReservationState.RELEASED:
                self._release(command)

    @staticmethod
    def _deadline(command: TrainingAdapterExtractRequest) -> None:
        if datetime.now(UTC) >= command.deadline:
            raise TimeoutError("Extraction deadline expired")

    def _hash(self, path: Path, command: TrainingAdapterExtractRequest, *, private: bool) -> str:
        digest = hashlib.sha256()
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise TrainingValidationError("Extraction input is linked")
        if not stat.S_ISREG(path.lstat().st_mode) or path.lstat().st_nlink != 1:
            raise TrainingValidationError("Extraction input is not an unlinked regular file")
        with private_open(path) if private else path.open("rb") as stream:
            while chunk := stream.read(CHUNK):
                self._deadline(command)
                digest.update(chunk)
        self._deadline(command)
        return digest.hexdigest()

    def _base_config(self, command: TrainingAdapterExtractRequest) -> dict[str, Any]:
        # ResolvedTrainingSpec carries a digest, not a caller-supplied filesystem path.
        matches = []
        for path in self.store.root.glob("*.manifest.json"):
            self._deadline(command)
            if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
                raise TrainingValidationError("Base manifest is unsafe")
            manifest = ChecksumManifest.model_validate_json(path.read_bytes())
            if manifest.sha256() == command.resolved.base_manifest_sha256:
                matches.append(manifest)
        if len(matches) != 1:
            raise TrainingValidationError("Exact acquired base is absent or ambiguous")
        manifest = matches[0]
        if (
            len({entry.path for entry in manifest.files}) != len(manifest.files)
            or sum(entry.bytes for entry in manifest.files) != manifest.total_bytes
        ):
            raise TrainingValidationError("Base manifest is incomplete")
        root = self.store.root / manifest.slug
        if root.is_symlink() or self.store.path_for(manifest.slug) != root:
            raise TrainingValidationError("Base directory is unsafe")
        config_entry = next(
            (entry for entry in manifest.files if entry.path == "config.json"), None
        )
        if config_entry is None or config_entry.bytes > 64 * 1024:
            raise TrainingValidationError("Base configuration is absent or oversized")
        for entry in manifest.files:
            relative = Path(entry.path)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise TrainingValidationError("Base manifest path escapes the acquired directory")
            path = root / entry.path
            if (
                path.stat().st_size != entry.bytes
                or self._hash(path, command, private=False) != entry.sha256
            ):
                raise TrainingValidationError("Exact acquired base checksum differs")
        config: dict[str, Any] = json.loads((root / "config.json").read_bytes())
        p = command.resolved.spec.parameterization
        if (
            config.get("model_file") is not None
            or config.get("auto_map")
            or config.get("model_type") not in APPROVED_ARCHITECTURES
        ):
            raise TrainingValidationError("Executable or unsupported base configuration")
        if config.get("quantization_config") not in (None, config.get("quantization")):
            raise TrainingValidationError("Incompatible legacy base quantization")
        quantization = config.get("quantization")
        if p.kind == "qlora":
            if (
                not isinstance(quantization, dict)
                or quantization.get("bits") != 4
                or quantization.get("group_size") != 64
                or quantization.get("mode", "affine") != "affine"
                or not set(quantization) <= {"bits", "group_size", "mode"}
            ):
                raise TrainingValidationError("QLoRA base quantization is incompatible")
        elif quantization is not None:
            raise TrainingValidationError("Dense adapter requires an unquantized base")
        if not set(p.target_modules) <= APPROVED_TARGETS:
            raise TrainingValidationError("Unsupported adapter target")
        dimensions = (
            "num_hidden_layers",
            "hidden_size",
            "intermediate_size",
            "num_attention_heads",
        )
        if any(
            type(config.get(key)) is not int or not 1 <= config[key] <= 2**20 for key in dimensions
        ):
            raise TrainingValidationError("Base dimensions are invalid")
        layers, hidden, intermediate, heads = (config[key] for key in dimensions)
        kv_heads = config.get("num_key_value_heads", heads)
        head_dim = config.get("head_dim", hidden // heads)
        if (
            type(kv_heads) is not int
            or type(head_dim) is not int
            or not 1 <= kv_heads <= heads
            or heads % kv_heads
            or not 1 <= head_dim <= 2**20
            or p.num_layers > layers
        ):
            raise TrainingValidationError("Base attention/layer dimensions are incompatible")
        shapes = {
            "self_attn.q_proj": (heads * head_dim, hidden),
            "self_attn.k_proj": (kv_heads * head_dim, hidden),
            "self_attn.v_proj": (kv_heads * head_dim, hidden),
            "self_attn.o_proj": (hidden, heads * head_dim),
            "mlp.gate_proj": (intermediate, hidden),
            "mlp.up_proj": (intermediate, hidden),
            "mlp.down_proj": (hidden, intermediate),
        }
        expected = {
            f"model.layers.{layer}.{target}.weight": shapes[target]
            for layer in range(layers - p.num_layers, layers)
            for target in p.target_modules
        }
        headers: dict[str, tuple[list[int], set[str]]] = {}
        for key, (output, input_) in expected.items():
            if p.kind == "qlora":
                if input_ % 64:
                    raise TrainingValidationError("Quantized target dimensions are incompatible")
                headers[key] = ([output, input_ // 8], {"U32"})
                for suffix in ("scales", "biases"):
                    headers[key.removesuffix("weight") + suffix] = (
                        [output, input_ // 64],
                        {"F32", "F16", "BF16"},
                    )
            else:
                headers[key] = ([output, input_], {"F32", "F16", "BF16"})
        found: set[str] = set()
        for entry in manifest.files:
            if not entry.path.endswith(".safetensors"):
                continue
            with safe_open(str(root / entry.path), framework="np") as header:
                for key in header.keys():  # noqa: SIM118 -- safetensors is not iterable
                    if key not in headers:
                        continue
                    shape, dtypes = headers[key]
                    dtype = header.get_slice(key).get_dtype()
                    if (
                        key in found
                        or header.get_slice(key).get_shape() != shape
                        or dtype not in dtypes
                    ):
                        raise TrainingValidationError(
                            "Base target tensor shape/dtype is incompatible"
                        )
                    found.add(key)
        if found != set(headers):
            raise TrainingValidationError("Base target tensors are missing")
        return {key.removesuffix(".weight"): value for key, value in expected.items()}

    def _validate(
        self, command: TrainingAdapterExtractRequest
    ) -> tuple[TrainingArtifactManifest, Path, bytes]:
        self._deadline(command)
        manifest = self.artifacts.manifest(command.checkpoint_id)
        resolved_digest = hashlib.sha256(
            canonical(command.resolved.model_dump(mode="json"))
        ).hexdigest()
        if (
            manifest.kind != "checkpoint"
            or manifest.canonical_sha256() != command.checkpoint_manifest_sha256
            or (manifest.job_id, manifest.attempt_id, manifest.fence)
            != (command.job_id, command.attempt_id, command.fence)
            or manifest.runtime_sha256 != command.resolved.runtime_sha256
            or manifest.resolved_spec_sha256 != resolved_digest
            or manifest.world_size
            != (2 if command.resolved.spec.placement.mode == "data_parallel" else 1)
        ):
            raise TrainingValidationError("Checkpoint immutable identity differs")
        for entry in manifest.files:
            if (
                self._hash(self.artifacts.file(manifest, entry.id), command, private=True)
                != entry.sha256
            ):
                raise TrainingValidationError("Checkpoint checksum differs")
        for rank in manifest.ranks:
            if any(
                len({item.key for item in tensors}) != len(tensors)
                for tensors in (rank.adapter_tensors, rank.optimizer_tensors)
            ):
                raise TrainingValidationError("Checkpoint tensor descriptors contain duplicates")
            if len({rank.adapter_file_id, rank.optimizer_file_id, rank.state_file_id}) != 3:
                raise TrainingValidationError("Checkpoint rank files are aliased")
            _check_tensor_header(
                self.artifacts.file(manifest, rank.adapter_file_id), rank.adapter_tensors
            )
            _check_tensor_header(
                self.artifacts.file(manifest, rank.optimizer_file_id), rank.optimizer_tensors
            )
            state = CheckpointWorkerState.model_validate_json(
                private_read(self.artifacts.file(manifest, rank.state_file_id), MAX_METADATA)
            )
            if (
                state.job_id,
                state.attempt_id,
                state.fence,
                state.rank,
                state.world_size,
                state.completed_update,
                state.runtime_sha256,
                state.resolved_spec_sha256,
                state.optimizer,
            ) != (
                manifest.job_id,
                manifest.attempt_id,
                manifest.fence,
                rank.rank,
                manifest.world_size,
                manifest.update,
                manifest.runtime_sha256,
                manifest.resolved_spec_sha256,
                command.resolved.spec.optim,
            ):
                raise TrainingValidationError("Checkpoint full-state identity differs")
        shapes = self._base_config(command)
        p = command.resolved.spec.parameterization
        rank0 = next(rank for rank in manifest.ranks if rank.rank == 0)
        expected = {}
        for key, (output, input_) in shapes.items():
            expected[f"{key}.lora_a"] = [input_, p.rank]
            expected[f"{key}.lora_b"] = [p.rank, output]
            if p.kind == "dora":
                expected[f"{key}.m"] = [output]
        for rank in manifest.ranks:
            actual = {item.key: item for item in rank.adapter_tensors}
            if set(actual) != set(expected) or any(
                actual[key].shape != shape or actual[key].dtype != "float32"
                for key, shape in expected.items()
            ):
                raise TrainingValidationError(
                    "Adapter tensors differ from pinned native parameterization"
                )
        config = canonical(
            {
                "fine_tune_type": "dora" if p.kind == "dora" else "lora",
                "num_layers": p.num_layers,
                "lora_parameters": {
                    "rank": p.rank,
                    "scale": p.scale,
                    "dropout": p.dropout,
                    "keys": p.target_modules,
                },
                "coire_base_model_id": str(command.resolved.spec.model.model_id),
                "coire_base_variant_id": str(command.resolved.spec.model.variant_id),
                "coire_base_manifest_sha256": command.resolved.base_manifest_sha256,
                "coire_checkpoint_id": str(command.checkpoint_id),
                "coire_checkpoint_manifest_sha256": command.checkpoint_manifest_sha256,
            }
        )
        return manifest, self.artifacts.file(manifest, rank0.adapter_file_id), config

    def extract(self, command: TrainingAdapterExtractRequest) -> TrainingAdapterExtractionStatus:
        command = TrainingAdapterExtractRequest.model_validate(command.model_dump(mode="json"))
        with self.lock, tracer.start_as_current_span("coire.node.training.adapter_extract"):
            if command.node != self.artifacts.node_name:
                raise TrainingConflict("Extraction targets another node")
            if self._path(command.command_id).exists():
                record = self._read(command.command_id)
                if record["request"] != command.model_dump(mode="json"):
                    raise TrainingConflict("Extraction command identifies different intent")
                return self.status(command.command_id)
            for path in self.root.glob("*.json"):
                prior = self._read(uuid.UUID(path.stem))
                if prior["request"]["adapter_id"] == str(command.adapter_id) or prior["request"][
                    "disk_reservation_id"
                ] == str(command.disk_reservation_id):
                    raise TrainingConflict(
                        "Extraction artifact or reservation identity is already owned"
                    )
            destination = self.artifacts.root / str(command.adapter_id)
            if destination.exists() or destination.is_symlink():
                raise TrainingConflict("Adapter artifact is immutable and already exists")
            status = TrainingAdapterExtractionStatus(
                command_id=command.command_id,
                adapter_id=command.adapter_id,
                checkpoint_id=command.checkpoint_id,
                node=command.node,
                state="queued",
            )
            record = {
                "request": command.model_dump(mode="json"),
                "status": status.model_dump(mode="json"),
            }
            self._save(record)  # Intent precedes hold; recovery validates the hold's exact payload.
            held = False
            published = False
            staging = self._staging(command)
            reason: TrainingReason = "checkpoint_invalid"
            try:
                self._deadline(command)
                checkpoint, source, config = self._validate(command)
                adapter_entry = next(
                    entry
                    for entry in checkpoint.files
                    if self.artifacts.file(checkpoint, entry.id) == source
                )
                files = [
                    TrainingArtifactFile(
                        id="adapter",
                        name="adapters.safetensors",
                        bytes=adapter_entry.bytes,
                        sha256=adapter_entry.sha256,
                    ),
                    TrainingArtifactFile(
                        id="config",
                        name="adapter_config.json",
                        bytes=len(config),
                        sha256=hashlib.sha256(config).hexdigest(),
                    ),
                ]
                manifest = TrainingArtifactManifest(
                    artifact_id=command.adapter_id,
                    kind="adapter",
                    files=files,
                    total_bytes=sum(entry.bytes for entry in files),
                    job_id=command.job_id,
                    attempt_id=command.attempt_id,
                    fence=command.fence,
                    update=checkpoint.update,
                    runtime_sha256=checkpoint.runtime_sha256,
                    resolved_spec_sha256=checkpoint.resolved_spec_sha256,
                )
                encoded = manifest.model_dump_json().encode()
                if manifest.total_bytes + len(encoded) > command.max_bytes:
                    raise ReservationRefused(
                        impossible=True,
                        required=manifest.total_bytes + len(encoded),
                        committed=0,
                        budget=command.max_bytes,
                    )
                reason = "disk_full"
                self._hold(command)
                held = True
                self._status(record, state="running")
                self._deadline(command)
                staging.mkdir(mode=0o700)
                digest = hashlib.sha256()
                with (
                    private_open(source) as input_,
                    (staging / "adapters.safetensors").open("xb") as output,
                ):
                    os.fchmod(output.fileno(), 0o600)
                    count = 0
                    while chunk := input_.read(CHUNK):
                        self._deadline(command)
                        count += len(chunk)
                        if count > adapter_entry.bytes:
                            raise TrainingValidationError("Adapter grew during extraction")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if count != adapter_entry.bytes or digest.hexdigest() != adapter_entry.sha256:
                    raise TrainingValidationError("Adapter changed during extraction")
                for name, data in (("adapter_config.json", config), ("manifest.json", encoded)):
                    with (staging / name).open("xb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                _fsync_directory(staging)
                record["publish_manifest"] = manifest.model_dump(mode="json")
                self._save(record)
                self._deadline(command)
                if destination.exists() or destination.is_symlink():
                    raise TrainingConflict("Adapter artifact was concurrently published")
                os.rename(staging, destination)
                published = True
                _fsync_directory(self.artifacts.root)
                self._status(record, state="succeeded", manifest=manifest.model_dump(mode="json"))
            except Exception as exc:
                if published:
                    raise  # Recovery owns uncertain post-rename/fsync outcomes; retain hold.
                if staging.exists() and not staging.is_symlink():
                    shutil.rmtree(staging)
                    _fsync_directory(self.artifacts.root)
                failure: TrainingReason = (
                    "execution_timeout"
                    if isinstance(exc, TimeoutError)
                    else "disk_full"
                    if isinstance(exc, ReservationRefused)
                    or (isinstance(exc, OSError) and exc.errno == 28)
                    else reason
                )
                self._status(record, state="failed", reason=failure)
            finally:
                if held and self.status(command.command_id).state in {"succeeded", "failed"}:
                    self._release(command)
            result = self.status(command.command_id)
            outcomes.add(
                1, {"node": command.node, "state": result.state, "reason": result.reason or "none"}
            )
            logger.info(
                "adapter extraction completed",
                extra={
                    "job_id": command.job_id,
                    "attempt_id": command.attempt_id,
                    "model_id": str(command.resolved.spec.model.model_id),
                    "adapter_id": str(command.adapter_id),
                    "state": result.state,
                    "reason": result.reason,
                },
            )
            return result
