"""Inert full-state checkpoints with immutable manifests and crash-durable local publication."""

from __future__ import annotations

import math
import os
import platform
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import ValidationError
from safetensors import safe_open

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.training_node import (
    CheckpointWorkerState,
    CheckpointWorkerStateDocument,
    CheckpointWorkerStateV3,
    MixtureSamplerState,
    RankStateManifest,
    SingleSourceSamplerState,
    TensorDescriptor,
    TrainingArtifactFile,
    TrainingArtifactManifest,
    parse_checkpoint_worker_state,
)
from coire_node.store import sha256_file

if TYPE_CHECKING:
    from coire_node.training.objectives import SftRuntime
    from coire_node.training.preference_data import PreferenceSampler
    from coire_node.training.sampler import TrainingSampler

MAX_STATE_BYTES = 64 * 1024**2
_HEADER_DTYPES = {
    "F32": "float32",
    "F16": "float16",
    "BF16": "bfloat16",
    "I32": "int32",
    "I64": "int64",
    "U32": "uint32",
    "U64": "uint64",
    "BOOL": "bool",
}


class TensorIO(Protocol):
    def is_tensor(self, value: object) -> bool: ...
    def shape(self, value: Any) -> list[int]: ...
    def dtype(self, value: Any) -> str: ...
    def save(self, path: Path, values: dict[str, Any]) -> None: ...
    def load(self, path: Path) -> dict[str, Any]: ...


class MlxTensorIO:
    @staticmethod
    def module() -> Any:
        if platform.node().lower().split(".", 1)[0] == "coire-core":
            raise TrainingValidationError("MLX checkpoint work is forbidden on core")
        import mlx.core as mx

        return mx

    def is_tensor(self, value: object) -> bool:
        return isinstance(value, self.module().array)

    def shape(self, value: Any) -> list[int]:
        return list(value.shape)

    def dtype(self, value: Any) -> str:
        return str(value.dtype).rsplit(".", 1)[-1].replace("bool_", "bool")

    def save(self, path: Path, values: dict[str, Any]) -> None:
        mx = self.module()
        mx.eval(*values.values())
        mx.save_safetensors(str(path), values)

    def load(self, path: Path) -> dict[str, Any]:
        return dict(self.module().load(str(path)))


@dataclass
class RestoredCheckpoint:
    state: CheckpointWorkerStateDocument
    adapter_tensors: dict[str, Any]
    optimizer_state: Any
    manifest: TrainingArtifactManifest


@dataclass(frozen=True)
class RankCheckpointComponent:
    """Private local component, never a complete/durable artifact or wire manifest."""

    directory: Path
    state: CheckpointWorkerState
    rank_manifest: RankStateManifest
    files: tuple[TrainingArtifactFile, ...]


def apply_restored_checkpoint(
    restored: RestoredCheckpoint,
    runtime: SftRuntime,
    optimizer: Any,
    sampler: TrainingSampler | PreferenceSampler,
    *,
    expected_optimizer: Any,
) -> None:
    """Validate the complete destination tree before restoring on the training thread.

    The caller reconstructs the optimizer and its schedule from immutable settings.
    Initializing its moments here supplies the exact expected tree; a missing moment
    must never trigger MLX's otherwise implicit fresh-state initialization on update.
    """
    from mlx.utils import tree_flatten

    if restored.state.optimizer != expected_optimizer:
        raise TrainingConflict("Checkpoint optimizer settings differ from the pinned run")
    codec = MlxTensorIO()
    current = {
        key: value for key, value in runtime.parameters().items() if key in runtime.trainable_keys
    }
    if _descriptors(current, codec) != _descriptors(restored.adapter_tensors, codec):
        raise TrainingValidationError("Checkpoint adapter does not match exact runtime parameters")
    getter = cast(Callable[[], dict[str, Any]], runtime.model.trainable_parameters)
    optimizer.init(getter())
    expected = dict(tree_flatten(optimizer.state))
    actual = dict(tree_flatten(restored.optimizer_state))
    if expected.keys() != actual.keys():
        raise TrainingValidationError(
            "Checkpoint optimizer keys differ from the full runtime state"
        )
    for key, value in expected.items():
        other = actual[key]
        if codec.is_tensor(value):
            if (
                not codec.is_tensor(other)
                or codec.shape(value) != codec.shape(other)
                or codec.dtype(value) != codec.dtype(other)
            ):
                raise TrainingValidationError(
                    "Checkpoint optimizer tensor differs from the runtime shape/dtype"
                )
        elif type(value) is not type(other):
            raise TrainingValidationError("Checkpoint optimizer structure differs from the runtime")
    _check_optimizer_step(restored.state, restored.optimizer_state, codec)
    from coire_node.training.preference_data import PreferenceSampler

    if isinstance(restored.state, CheckpointWorkerStateV3):
        if not isinstance(sampler, PreferenceSampler):
            raise TrainingConflict("Preference checkpoint requires its paired sampler")
        sampler.restore(restored.state.sampler)
    else:
        if isinstance(sampler, PreferenceSampler):
            raise TrainingConflict("SFT checkpoint cannot restore a preference sampler")
        sampler.restore(restored.state.sampler)
    runtime.model.load_weights(list(restored.adapter_tensors.items()), strict=False)
    optimizer.state = restored.optimizer_state
    restore_mlx_rng_key(restored.state.mlx_rng_key)


def capture_mlx_rng_key() -> tuple[int, int]:
    """Capture the training thread's current key after synchronizing lazy state."""
    mx = MlxTensorIO.module()
    key = mx.random.state[0]
    mx.eval(key)
    words = key.tolist()
    if (
        not isinstance(words, list)
        or len(words) != 2
        or any(type(word) is not int or not 0 <= word < 2**32 for word in words)
    ):
        raise TrainingValidationError("MLX random state does not match the pinned key format")
    return words[0], words[1]


def restore_mlx_rng_key(words: tuple[int, int]) -> None:
    """Restore the saved current key through MLX 0.32.2's public seed API."""
    if len(words) != 2 or any(type(word) is not int or not 0 <= word < 2**32 for word in words):
        raise TrainingValidationError("Checkpoint random key is invalid")
    mx = MlxTensorIO.module()
    mx.random.seed((words[0] << 32) | words[1])
    if capture_mlx_rng_key() != words:
        raise TrainingValidationError("Pinned MLX random-key restoration failed")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise TrainingValidationError("Checkpoint file is absent or inaccessible") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise TrainingValidationError("Checkpoint file is linked, public or not a regular file")


def _finish_file(path: Path) -> None:
    path.chmod(0o600)
    _private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _descriptors(values: dict[str, Any], codec: TensorIO) -> list[TensorDescriptor]:
    descriptors = []
    for key, value in sorted(values.items()):
        if not codec.is_tensor(value):
            raise TrainingValidationError("Checkpoint tensor map contains a non-tensor")
        descriptors.append(
            TensorDescriptor.model_validate(
                {"key": key, "shape": codec.shape(value), "dtype": codec.dtype(value)}
            )
        )
    return descriptors


def _encode_tree(
    value: Any, codec: TensorIO, arrays: dict[str, Any], *, depth: int = 0
) -> dict[str, Any]:
    if depth > 64 or len(arrays) >= 200_000:
        raise TrainingValidationError("Optimizer checkpoint tree exceeds its bound")
    if codec.is_tensor(value):
        key = f"optimizer-{len(arrays):06d}"
        arrays[key] = value
        return {"kind": "tensor", "tensor": key}
    if isinstance(value, dict):
        if any(not isinstance(key, str) or len(key) > 512 for key in value):
            raise TrainingValidationError("Optimizer tree keys must be bounded strings")
        return {
            "kind": "mapping",
            "items": {
                key: _encode_tree(child, codec, arrays, depth=depth + 1)
                for key, child in value.items()
            },
        }
    if isinstance(value, (list, tuple)):
        return {
            "kind": "tuple" if isinstance(value, tuple) else "list",
            "items": [_encode_tree(child, codec, arrays, depth=depth + 1) for child in value],
        }
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TrainingValidationError("Optimizer checkpoint primitives must be finite")
        return {"kind": "value", "value": value}
    raise TrainingValidationError("Optimizer state contains executable or unsupported objects")


def _decode_tree(node: Any, arrays: dict[str, Any], *, depth: int = 0) -> Any:
    if depth > 64 or not isinstance(node, dict):
        raise TrainingValidationError("Optimizer checkpoint structure is malformed")
    kind = node.get("kind")
    if not isinstance(kind, str):
        raise TrainingValidationError("Optimizer checkpoint node kind is invalid")
    if kind == "tensor" and set(node) == {"kind", "tensor"}:
        if not isinstance(node["tensor"], str) or node["tensor"] not in arrays:
            raise TrainingValidationError("Optimizer checkpoint references a missing tensor")
        return arrays[node["tensor"]]
    if kind == "mapping" and set(node) == {"kind", "items"} and isinstance(node["items"], dict):
        return {
            key: _decode_tree(value, arrays, depth=depth + 1)
            for key, value in node["items"].items()
        }
    if (
        kind in {"list", "tuple"}
        and set(node) == {"kind", "items"}
        and isinstance(node["items"], list)
    ):
        items = [_decode_tree(value, arrays, depth=depth + 1) for value in node["items"]]
        return tuple(items) if kind == "tuple" else items
    if (
        kind == "value"
        and set(node) == {"kind", "value"}
        and (node["value"] is None or isinstance(node["value"], (str, bool, int, float)))
    ):
        return node["value"]
    raise TrainingValidationError("Optimizer checkpoint contains an unknown serialized node")


def _check_tensor_header(path: Path, expected: list[TensorDescriptor]) -> None:
    try:
        with safe_open(str(path), framework="np") as header:
            if set(header.keys()) != {item.key for item in expected}:
                raise TrainingValidationError("Checkpoint tensor keys differ from their manifest")
            for item in expected:
                view = header.get_slice(item.key)
                if (
                    view.get_shape() != item.shape
                    or _HEADER_DTYPES.get(view.get_dtype()) != item.dtype
                ):
                    raise TrainingValidationError(
                        "Checkpoint tensor shape/dtype differs from its manifest"
                    )
    except TrainingValidationError:
        raise
    except Exception:
        raise TrainingValidationError("Checkpoint tensor header is invalid") from None


def _check_optimizer_step(
    state: CheckpointWorkerStateDocument, optimizer: Any, codec: TensorIO
) -> None:
    if not isinstance(optimizer, dict) or "step" not in optimizer:
        raise TrainingValidationError("Checkpoint requires the full optimizer update counter")
    step = optimizer["step"]
    if codec.is_tensor(step):
        if codec.shape(step) != [] or codec.dtype(step) not in {
            "uint32",
            "uint64",
            "int32",
            "int64",
        }:
            raise TrainingValidationError("Optimizer update counter must be an integer scalar")
        step = step.item()
    if type(step) is not int or step != state.completed_update:
        raise TrainingValidationError(
            "Optimizer update counter differs from completed checkpoint update"
        )


def _bound_rank_state(
    manifest: TrainingArtifactManifest, rank: RankStateManifest, paths: dict[str, Path]
) -> CheckpointWorkerStateDocument:
    path = paths[rank.state_file_id]
    if path.stat().st_size > MAX_STATE_BYTES:
        raise TrainingValidationError("Checkpoint state exceeds its byte bound")
    try:
        import json

        state = parse_checkpoint_worker_state(json.loads(path.read_bytes()))
    except (ValidationError, ValueError):
        raise TrainingValidationError("Checkpoint full-state metadata is invalid") from None
    if (
        state.runtime_sha256 != manifest.runtime_sha256
        or state.resolved_spec_sha256 != manifest.resolved_spec_sha256
        or state.completed_update != manifest.update
        or state.job_id != manifest.job_id
        or state.attempt_id != manifest.attempt_id
        or state.fence != manifest.fence
        or state.rank != rank.rank
        or state.world_size != manifest.world_size
        or (
            isinstance(state.sampler, MixtureSamplerState)
            and (state.sampler.rank != rank.rank or state.sampler.world_size != manifest.world_size)
        )
    ):
        raise TrainingValidationError("Checkpoint state identity differs from its manifest")
    return state


class CheckpointStore:
    """Local complete bundles are staged observations, not controller durability acknowledgement."""

    def __init__(
        self,
        root: Path,
        *,
        tensor_io: TensorIO | None = None,
        disk_floor_bytes: int = 20 * 1024**3,
        max_bytes: int = 20 * 1024**3,
    ) -> None:
        if root.is_symlink():
            raise TrainingValidationError("Checkpoint store must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve()
        self.tensor_io = tensor_io or MlxTensorIO()
        self.disk_floor_bytes = disk_floor_bytes
        self.max_bytes = max_bytes

    def path_for(self, artifact_id: uuid.UUID) -> Path:
        if not isinstance(artifact_id, uuid.UUID):
            raise TrainingValidationError("Checkpoint artifact identity must be a UUID")
        path = self.root / str(artifact_id)
        if path.is_symlink():
            raise TrainingValidationError("Checkpoint artifact path is linked")
        return path

    def save_rank(
        self,
        state: CheckpointWorkerState,
        adapter: dict[str, Any],
        optimizer_state: Any,
        *,
        artifact_id: uuid.UUID,
    ) -> RankCheckpointComponent:
        """Stage one rank for an external collector; no partial manifest is advertised."""
        self.path_for(artifact_id)
        state = CheckpointWorkerState.model_validate(state.model_dump(mode="json"))
        if isinstance(state.sampler, MixtureSamplerState) and (
            state.sampler.rank != state.rank or state.sampler.world_size != state.world_size
        ):
            raise TrainingConflict("Checkpoint sampler scope differs from rank state")
        _check_optimizer_step(state, optimizer_state, self.tensor_io)
        arrays: dict[str, Any] = {}
        tree = _encode_tree(optimizer_state, self.tensor_io, arrays)
        if not adapter or not arrays:
            raise TrainingValidationError("Full rank state requires adapter and optimizer tensors")
        state = state.model_copy(update={"optimizer_tree": tree})
        encoded = state.model_dump_json().encode()
        if len(encoded) > MAX_STATE_BYTES:
            raise TrainingValidationError("Checkpoint metadata exceeds its byte bound")
        prefix = f"rank-{state.rank}"
        rank = RankStateManifest(
            rank=state.rank,
            update=state.completed_update,
            adapter_file_id=f"{prefix}-adapter",
            optimizer_file_id=f"{prefix}-optimizer",
            state_file_id=f"{prefix}-state",
            adapter_tensors=_descriptors(adapter, self.tensor_io),
            optimizer_tensors=_descriptors(arrays, self.tensor_io),
        )
        estimated = len(encoded) + sum(
            math.prod(item.shape) * 8 + len(item.key) * 4 + 512
            for item in [*rank.adapter_tensors, *rank.optimizer_tensors]
        )
        used = sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if (
            used + estimated > self.max_bytes
            or shutil.disk_usage(self.root).free - estimated < self.disk_floor_bytes
        ):
            raise TrainingValidationError("Rank checkpoint disk headroom is unavailable")
        parent = self.root / f".rank-components-{artifact_id}"
        if parent.is_symlink():
            raise TrainingValidationError("Rank staging path is linked")
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise TrainingValidationError("Rank staging directory must be private and owned")
        destination = parent / prefix
        if destination.exists():
            raise TrainingConflict("Rank checkpoint component is immutable")
        staging = Path(tempfile.mkdtemp(prefix=f".{prefix}.", dir=parent))
        try:
            files = []
            for file_id, values in (
                (rank.adapter_file_id, adapter),
                (rank.optimizer_file_id, arrays),
            ):
                path = staging / f"{file_id}.safetensors"
                self.tensor_io.save(path, values)
                _finish_file(path)
                files.append(
                    TrainingArtifactFile(
                        id=file_id,
                        name=path.name,
                        bytes=path.stat().st_size,
                        sha256=sha256_file(path),
                    )
                )
            state_path = staging / f"{rank.state_file_id}.json"
            state_path.write_bytes(encoded)
            _finish_file(state_path)
            files.append(
                TrainingArtifactFile(
                    id=rank.state_file_id,
                    name=state_path.name,
                    bytes=len(encoded),
                    sha256=sha256_file(state_path),
                )
            )
            if used + sum(item.bytes for item in files) > self.max_bytes:
                raise TrainingValidationError("Rank checkpoint exceeds disk reservation")
            _fsync_directory(staging)
            if destination.exists():
                raise TrainingConflict("Rank component was concurrently published")
            os.rename(staging, destination)
            _fsync_directory(parent)
            _fsync_directory(self.root)
            return RankCheckpointComponent(destination, state, rank, tuple(files))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def publish_rank_bundle(
        self, components: Sequence[RankCheckpointComponent], *, artifact_id: uuid.UUID
    ) -> TrainingArtifactManifest:
        """Publish only a verified common complete bundle after external rank collection.

        The collector owns coordination, fencing, component transport, and cleanup;
        both-copy acknowledgement remains a separate controller operation.
        """
        if not components:
            raise TrainingValidationError("Checkpoint requires rank components")
        states = [
            CheckpointWorkerState.model_validate(c.state.model_dump(mode="json"))
            for c in components
        ]
        first = states[0]
        if len(states) != first.world_size or {s.rank for s in states} != set(
            range(first.world_size)
        ):
            raise TrainingConflict("Checkpoint rank collection is incomplete or duplicated")
        fields = (
            "job_id",
            "attempt_id",
            "fence",
            "completed_update",
            "world_size",
            "runtime_sha256",
            "resolved_spec_sha256",
            "optimizer",
        )
        if any(any(getattr(s, key) != getattr(first, key) for key in fields) for s in states):
            raise TrainingConflict("Checkpoint ranks disagree on common update/input lineage")
        if any(type(s.sampler) is not type(first.sampler) for s in states):
            raise TrainingConflict("Checkpoint rank sampler algorithms disagree")
        if isinstance(first.sampler, SingleSourceSamplerState) and any(
            s.sampler != first.sampler for s in states
        ):
            raise TrainingConflict("Checkpoint ranks disagree on global single-source cursor")
        if isinstance(first.sampler, MixtureSamplerState) and any(
            not isinstance(s.sampler, MixtureSamplerState)
            or (s.sampler.identity_sha256, s.sampler.epoch, s.sampler.cursor)
            != (first.sampler.identity_sha256, first.sampler.epoch, first.sampler.cursor)
            for s in states
        ):
            raise TrainingConflict("Checkpoint ranks disagree on global mixture cursor")
        manifest = TrainingArtifactManifest(
            artifact_id=artifact_id,
            kind="checkpoint",
            files=[file for component in components for file in component.files],
            total_bytes=sum(file.bytes for component in components for file in component.files),
            job_id=first.job_id,
            attempt_id=first.attempt_id,
            fence=first.fence,
            update=first.completed_update,
            world_size=first.world_size,
            runtime_sha256=first.runtime_sha256,
            resolved_spec_sha256=first.resolved_spec_sha256,
            ranks=[c.rank_manifest for c in components],
        )
        destination = self.path_for(artifact_id)
        if destination.exists():
            raise TrainingConflict("Checkpoint artifact is immutable and already exists")
        encoded_manifest = manifest.model_dump_json().encode()
        used = sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        required = manifest.total_bytes + len(encoded_manifest)
        if (
            used + required > self.max_bytes
            or shutil.disk_usage(self.root).free - required < self.disk_floor_bytes
        ):
            raise TrainingValidationError("Common checkpoint disk headroom is unavailable")
        staging = Path(tempfile.mkdtemp(prefix=f".{artifact_id}.", dir=self.root))
        try:
            for component, state in zip(components, states, strict=True):
                paths = {}
                for file in component.files:
                    source = component.directory / file.name
                    if any(path.is_symlink() for path in (source, *source.parents)):
                        raise TrainingValidationError("Rank component path is linked")
                    _private_file(source)
                    target = staging / file.name
                    shutil.copyfile(source, target)
                    _finish_file(target)
                    if target.stat().st_size != file.bytes or sha256_file(target) != file.sha256:
                        raise TrainingValidationError("Collected rank bytes differ from component")
                    paths[file.id] = target
                rank = component.rank_manifest
                _check_tensor_header(paths[rank.adapter_file_id], rank.adapter_tensors)
                _check_tensor_header(paths[rank.optimizer_file_id], rank.optimizer_tensors)
                if (
                    CheckpointWorkerState.model_validate_json(
                        paths[rank.state_file_id].read_bytes()
                    )
                    != state
                ):
                    raise TrainingConflict("Collected rank metadata differs from frozen component")
                if rank.rank != state.rank or rank.update != state.completed_update:
                    raise TrainingConflict("Collected rank descriptor differs from its state")
                if isinstance(state.sampler, MixtureSamplerState) and (
                    state.sampler.rank != state.rank or state.sampler.world_size != state.world_size
                ):
                    raise TrainingConflict("Collected sampler scope differs from rank state")
            path = staging / "manifest.json"
            path.write_bytes(encoded_manifest)
            _finish_file(path)
            _fsync_directory(staging)
            if destination.exists():
                raise TrainingConflict("Checkpoint artifact was concurrently published")
            os.rename(staging, destination)
            _fsync_directory(self.root)
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def save(
        self,
        state: CheckpointWorkerStateDocument,
        adapter: dict[str, Any],
        optimizer_state: Any,
        *,
        artifact_id: uuid.UUID | None = None,
    ) -> TrainingArtifactManifest:
        state = parse_checkpoint_worker_state(state.model_dump(mode="json"))
        if isinstance(state.sampler, MixtureSamplerState) and (
            state.sampler.rank != state.rank or state.sampler.world_size != state.world_size
        ):
            raise TrainingConflict("Checkpoint sampler scope differs from rank state")
        if state.world_size != 1 or state.rank != 0:
            raise TrainingValidationError(
                "Distributed checkpoint requires the common-rank commit path"
            )
        artifact_id = artifact_id or uuid.uuid4()
        destination = self.path_for(artifact_id)
        if destination.exists():
            raise TrainingConflict("Checkpoint artifact is immutable and already exists")
        _check_optimizer_step(state, optimizer_state, self.tensor_io)
        optimizer_arrays: dict[str, Any] = {}
        tree = _encode_tree(optimizer_state, self.tensor_io, optimizer_arrays)
        if not optimizer_arrays or not adapter:
            raise TrainingValidationError("Full checkpoint requires adapter and optimizer tensors")
        state = state.model_copy(update={"optimizer_tree": tree})
        encoded = state.model_dump_json().encode()
        if len(encoded) > MAX_STATE_BYTES:
            raise TrainingValidationError("Checkpoint metadata exceeds its byte bound")
        adapter_descriptors = _descriptors(adapter, self.tensor_io)
        optimizer_descriptors = _descriptors(optimizer_arrays, self.tensor_io)
        widths = {
            "float32": 4,
            "float16": 2,
            "bfloat16": 2,
            "uint32": 4,
            "int32": 4,
            "int64": 8,
            "uint64": 8,
            "bool": 1,
        }
        descriptions = adapter_descriptors + optimizer_descriptors
        estimated = len(encoded) + sum(
            math.prod(item.shape) * widths[item.dtype]
            + len(item.key) * 4
            + len(item.shape) * 24
            + 256
            for item in descriptions
        )
        used = sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if (
            used + estimated > self.max_bytes
            or shutil.disk_usage(self.root).free - estimated < self.disk_floor_bytes
        ):
            raise TrainingValidationError("Checkpoint disk quota or safety headroom is unavailable")
        staging = Path(tempfile.mkdtemp(prefix=f".{artifact_id}.", dir=self.root))
        try:
            files = []
            for file_id, name, values in (
                ("rank-0-adapter", "rank-0-adapter.safetensors", adapter),
                ("rank-0-optimizer", "rank-0-optimizer.safetensors", optimizer_arrays),
            ):
                path = staging / name
                self.tensor_io.save(path, values)
                _finish_file(path)
                files.append(
                    TrainingArtifactFile(
                        id=file_id, name=name, bytes=path.stat().st_size, sha256=sha256_file(path)
                    )
                )
            state_path = staging / "rank-0-state.json"
            with state_path.open("xb") as output:
                output.write(encoded)
            _finish_file(state_path)
            files.append(
                TrainingArtifactFile(
                    id="rank-0-state",
                    name=state_path.name,
                    bytes=len(encoded),
                    sha256=sha256_file(state_path),
                )
            )
            manifest = TrainingArtifactManifest(
                artifact_id=artifact_id,
                kind="checkpoint",
                files=files,
                total_bytes=sum(item.bytes for item in files),
                job_id=state.job_id,
                attempt_id=state.attempt_id,
                fence=state.fence,
                update=state.completed_update,
                world_size=1,
                runtime_sha256=state.runtime_sha256,
                resolved_spec_sha256=state.resolved_spec_sha256,
                ranks=[
                    RankStateManifest(
                        rank=0,
                        update=state.completed_update,
                        adapter_file_id="rank-0-adapter",
                        optimizer_file_id="rank-0-optimizer",
                        state_file_id="rank-0-state",
                        adapter_tensors=adapter_descriptors,
                        optimizer_tensors=optimizer_descriptors,
                    )
                ],
            )
            if used + manifest.total_bytes > self.max_bytes:
                raise TrainingValidationError(
                    "Checkpoint serialized bytes exceed their reservation"
                )
            manifest_bytes = manifest.model_dump_json().encode()
            if used + manifest.total_bytes + len(manifest_bytes) > self.max_bytes:
                raise TrainingValidationError("Checkpoint manifest exceeds its disk reservation")
            manifest_path = staging / "manifest.json"
            with manifest_path.open("xb") as output:
                output.write(manifest_bytes)
            _finish_file(manifest_path)
            _fsync_directory(staging)
            if destination.exists():
                raise TrainingConflict("Checkpoint artifact was concurrently published")
            os.rename(staging, destination)
            _fsync_directory(self.root)
            return manifest
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def manifest(self, artifact_id: uuid.UUID) -> TrainingArtifactManifest:
        directory = self.path_for(artifact_id)
        if not directory.is_dir():
            raise TrainingValidationError("Checkpoint artifact is incomplete or absent")
        path = directory / "manifest.json"
        _private_file(path)
        if path.stat().st_size > MAX_STATE_BYTES:
            raise TrainingValidationError("Checkpoint manifest exceeds its bound")
        try:
            value = TrainingArtifactManifest.model_validate_json(path.read_bytes())
        except (ValueError, OSError):
            raise TrainingValidationError("Checkpoint manifest is invalid") from None
        if value.artifact_id != artifact_id or value.kind != "checkpoint":
            raise TrainingValidationError("Checkpoint manifest identity differs")
        return value

    def restore(
        self,
        artifact_id: uuid.UUID,
        *,
        expected_runtime_sha256: str,
        expected_resolved_spec_sha256: str,
        rank: int = 0,
        expected_world_size: int = 1,
    ) -> RestoredCheckpoint:
        manifest = self.manifest(artifact_id)
        if (
            manifest.runtime_sha256 != expected_runtime_sha256
            or manifest.resolved_spec_sha256 != expected_resolved_spec_sha256
        ):
            raise TrainingConflict("Checkpoint runtime or immutable inputs are incompatible")
        if (
            type(rank) is not int
            or type(expected_world_size) is not int
            or expected_world_size not in (1, 2)
            or not 0 <= rank < expected_world_size
            or manifest.world_size != expected_world_size
        ):
            raise TrainingConflict("Checkpoint world size/rank differs from pinned restore")
        directory = self.path_for(artifact_id)
        paths = {item.id: directory / item.name for item in manifest.files}
        for item in manifest.files:
            path = paths[item.id]
            _private_file(path)
            if path.stat().st_size != item.bytes or sha256_file(path) != item.sha256:
                raise TrainingValidationError("Checkpoint bytes differ from the committed manifest")
        selected_rank = next(item for item in manifest.ranks if item.rank == rank)
        states = {}
        for component in manifest.ranks:
            _check_tensor_header(paths[component.adapter_file_id], component.adapter_tensors)
            _check_tensor_header(paths[component.optimizer_file_id], component.optimizer_tensors)
            states[component.rank] = _bound_rank_state(manifest, component, paths)
        state = states[rank]
        if any(other.optimizer != state.optimizer for other in states.values()):
            raise TrainingValidationError("Checkpoint rank optimizer settings disagree")
        if isinstance(state.sampler, SingleSourceSamplerState) and any(
            other.sampler != state.sampler for other in states.values()
        ):
            raise TrainingValidationError("Checkpoint rank single-source cursors disagree")
        if isinstance(state.sampler, MixtureSamplerState):
            sampler = state.sampler
            if any(
                not isinstance(other.sampler, MixtureSamplerState)
                or (other.sampler.identity_sha256, other.sampler.epoch, other.sampler.cursor)
                != (sampler.identity_sha256, sampler.epoch, sampler.cursor)
                for other in states.values()
            ):
                raise TrainingValidationError("Checkpoint rank mixture cursors disagree")
        adapter = self.tensor_io.load(paths[selected_rank.adapter_file_id])
        optimizer_arrays = self.tensor_io.load(paths[selected_rank.optimizer_file_id])
        optimizer = _decode_tree(state.optimizer_tree, optimizer_arrays)
        _check_optimizer_step(state, optimizer, self.tensor_io)
        return RestoredCheckpoint(state, adapter, optimizer, manifest)

    def prune(
        self,
        *,
        committed_ids: Sequence[uuid.UUID],
        protected_ids: set[uuid.UUID],
        keep_last: int = 3,
    ) -> list[uuid.UUID]:
        if not 1 <= keep_last <= 3:
            raise TrainingValidationError("Checkpoint retention count is outside its bound")
        manifests = []
        for item in committed_ids:
            try:
                manifest = self.manifest(item)
                directory = self.path_for(item)
                for entry in manifest.files:
                    file = directory / entry.name
                    _private_file(file)
                    if file.stat().st_size != entry.bytes or sha256_file(file) != entry.sha256:
                        raise TrainingValidationError("Checkpoint retention found corrupt bytes")
                manifests.append((item, manifest))
            except TrainingValidationError:
                # Corrupt candidates cannot displace the last complete valid recovery point.
                protected_ids = protected_ids | {item}
        ordered = sorted(manifests, key=lambda pair: pair[1].update or 0, reverse=True)
        keep = protected_ids | {item for item, _manifest in ordered[:keep_last]}
        removed = []
        for item, _manifest in ordered:
            if item not in keep:
                shutil.rmtree(self.path_for(item))
                removed.append(item)
        _fsync_directory(self.root)
        return removed

    def newest_valid(
        self,
        committed_ids: Sequence[uuid.UUID],
        *,
        expected_runtime_sha256: str,
        expected_resolved_spec_sha256: str,
        rank: int = 0,
        expected_world_size: int = 1,
    ) -> RestoredCheckpoint:
        candidates = []
        for item in committed_ids:
            try:
                candidates.append((item, self.manifest(item)))
            except TrainingValidationError:
                continue
        for item, _manifest in sorted(
            candidates, key=lambda pair: pair[1].update or 0, reverse=True
        ):
            try:
                return self.restore(
                    item,
                    expected_runtime_sha256=expected_runtime_sha256,
                    expected_resolved_spec_sha256=expected_resolved_spec_sha256,
                    rank=rank,
                    expected_world_size=expected_world_size,
                )
            except (TrainingValidationError, TrainingConflict):
                continue
        raise TrainingValidationError("No compatible complete committed checkpoint remains")
