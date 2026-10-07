"""Completed-update hooks around the unchanged bare mlx-lm train/evaluate functions.

The execution adapter is deliberately separate from registry resolution. A node must
bind validated local model and split assets before supplying a runtime and samplers.
No CLI option accepts a model path, dataset path, import string or trainer overrides.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import platform
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from opentelemetry import metrics, trace
from pydantic import BaseModel, TypeAdapter

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisBinding,
    SplitManifest,
    TokenizedTrainingExample,
)
from coire_core.models.training import TrainingMetricSample, TrainingOptimizer, TrainingReason
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgement,
    CheckpointWorkerState,
    NodeCheckpointPayload,
    NodeControlPayload,
    NodeProgressPayload,
    NodeTrainingEvent,
    TrainingArtifactManifest,
    TrainingLeaseRenewal,
    TrainingPauseRequest,
    TrainingPrepareRequest,
    TrainingStartReceipt,
    TrainingStartRequest,
)
from coire_core.models.training_types import TrainingId
from coire_core.training_data import compile_mixture, normalize_row, split_digest
from coire_node.footprint import phys_footprint
from coire_node.store import Store, write_atomic
from coire_node.training.checkpoints import (
    CheckpointStore,
    apply_restored_checkpoint,
    capture_mlx_rng_key,
    restore_mlx_rng_key,
)
from coire_node.training.datasets import index_training_source, load_analysis_tokenizer
from coire_node.training.distributed import (
    BareCollective,
    Collective,
    DeadlineGuardian,
    PrivateRankTransport,
    RankCheckpointCoordinator,
    RankTransport,
    parameter_digest,
)
from coire_node.training.guard import ExecutionGuard
from coire_node.training.journal import TrainingJournal, command_digest
from coire_node.training.loss import masked_sft_loss
from coire_node.training.objectives import SftRuntime, load_sft_runtime, validate_sft_input
from coire_node.training.rendering import ChatTokenizer, render_example
from coire_node.training.sampler import MixtureSampler, SingleSourceSampler, TrainingSampler

tracer = trace.get_tracer("coire.node.training")
logger = logging.getLogger(__name__)
updates = metrics.get_meter("coire.node.training").create_counter(
    "coire_training_completed_updates_total"
)


def payload_sha256(value: BaseModel) -> str:
    """Same strict canonical identity used by the controller's resolved analysis."""
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def read_private(path: Path, maximum: int) -> bytes:
    if any(parent.is_symlink() for parent in path.parents):
        raise TrainingValidationError("Private training path has a linked ancestor")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise TrainingValidationError("Private training file is unsafe or oversized")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(maximum + 1)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class FrozenInputs:
    """Internal node asset binding. Each persisted component is an existing core model."""

    binding: DatasetAnalysisBinding
    split: SplitManifest
    analysis: DatasetAnalysis
    source: Path


def validate_frozen_inputs(prepared: TrainingPrepareRequest, inputs: FrozenInputs) -> None:
    resolved = prepared.resolved
    spec = resolved.spec
    binding, split, analysis = inputs.binding, inputs.split, inputs.analysis
    selected = next(
        (item for item in resolved.datasets if item.dataset_id == binding.dataset_id), None
    )
    if selected is None:
        raise TrainingConflict("Input source is not part of resolved intent")
    source = next(
        (item for item in spec.data.train.datasets if item.dataset_id == binding.dataset_id), None
    )
    if (source is None and binding.dataset_id not in spec.data.validation.dataset_ids) or (
        source is not None and source.sample_count > len(split.train_rows)
    ):
        raise TrainingConflict("Selected pools differ from the frozen source split")
    if (
        binding.model_id != spec.model.model_id
        or binding.variant_id != spec.model.variant_id
        or binding.base_manifest_sha256 != resolved.base_manifest_sha256
        or binding.dataset_id != selected.dataset_id
        or binding.source_sha256 != selected.source_sha256
        or binding.split_sha256 != selected.split_sha256
        or binding.enable_thinking != resolved.enable_thinking
        or split.dataset_id != selected.dataset_id
        or split.source_sha256 != selected.source_sha256
        or split_digest(split) != selected.split_sha256
    ):
        raise TrainingConflict("Model/source/split binding differs from immutable training intent")
    if (
        analysis.id != selected.analysis_id
        or analysis.dataset_id != selected.dataset_id
        or analysis.model_id != spec.model.model_id
        or analysis.variant_id != spec.model.variant_id
        or analysis.state != "succeeded"
        or analysis.row_count != len(split.row_content_sha256)
        or analysis.tokenizer_sha256 != resolved.tokenizer_sha256
        or analysis.template_sha256 != resolved.template_sha256
        or analysis.runtime_sha256 != resolved.runtime_sha256
        or payload_sha256(analysis) != selected.analysis_sha256
    ):
        raise TrainingConflict("Training requires the exact successful frozen analysis")
    if (
        binding.template_override is not None
        and hashlib.sha256(binding.template_override.encode()).hexdigest()
        != resolved.template_sha256
    ):
        raise TrainingConflict("Frozen effective template content differs from its digest")


def load_frozen_inputs(
    prepared: TrainingPrepareRequest,
    directory: Path,
    journal: TrainingJournal,
    source_id: uuid.UUID | None = None,
) -> FrozenInputs:
    value = journal.get(prepared.attempt_id)
    expected = (
        value.get("input_files")
        if source_id is None
        else value.get("input_sources", {}).get(str(source_id))
    )
    if source_id is not None:
        directory = directory / str(source_id)
    if not isinstance(expected, dict) or set(expected) != {
        "binding.json",
        "split.json",
        "analysis.json",
        "source.jsonl",
    }:
        raise TrainingConflict("Node-owned frozen inputs have not been staged")
    metadata: dict[str, bytes] = {}
    for name in ("binding.json", "split.json", "analysis.json"):
        encoded = read_private(directory / name, 96 * 1024**2)
        if hashlib.sha256(encoded).hexdigest() != expected[name]:
            raise TrainingConflict("Node-owned input metadata changed after staging")
        metadata[name] = encoded
    source = directory / "source.jsonl"
    # Stream source integrity separately; do not allocate the entire uploaded corpus.
    verify_source(source, expected["source.jsonl"])
    inputs = FrozenInputs(
        DatasetAnalysisBinding.model_validate_json(metadata["binding.json"]),
        SplitManifest.model_validate_json(metadata["split.json"]),
        DatasetAnalysis.model_validate_json(metadata["analysis.json"]),
        source,
    )
    validate_frozen_inputs(prepared, inputs)
    return inputs


def load_all_frozen_inputs(
    prepared: TrainingPrepareRequest, directory: Path, journal: TrainingJournal
) -> list[FrozenInputs]:
    if journal.get(prepared.attempt_id).get("input_files") is not None:
        if len(prepared.resolved.datasets) != 1:
            raise TrainingConflict("Legacy input staging cannot cover multiple resolved sources")
        return [load_frozen_inputs(prepared, directory, journal)]
    return [
        load_frozen_inputs(prepared, directory, journal, selected.dataset_id)
        for selected in prepared.resolved.datasets
    ]


def verify_source(source: Path, expected: str) -> int:
    if any(path.is_symlink() for path in (source, *source.parents)):
        raise TrainingValidationError("Training source path is linked")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= 256 * 1024**2
        ):
            raise TrainingValidationError("Training source is unsafe or oversized")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise TrainingConflict("Training source digest differs from frozen revision")
        return info.st_size
    finally:
        os.close(fd)


def _compile_single_samples(
    prepared: TrainingPrepareRequest,
    inputs: FrozenInputs,
    tokenizer: ChatTokenizer,
    *,
    buffer_observer: Callable[[int], None] | None = None,
) -> tuple[SingleSourceSampler, SingleSourceSampler]:
    validate_frozen_inputs(prepared, inputs)
    if (
        len(prepared.resolved.spec.data.train.datasets) != 1
        or prepared.resolved.spec.data.train.replacement
    ):
        raise TrainingValidationError(
            "Mixture execution requires the shared checkpoint sampler state"
        )
    verify_source(inputs.source, inputs.binding.source_sha256)
    spec = prepared.resolved.spec
    selected_rows = sorted(inputs.split.train_rows)[: spec.data.train.datasets[0].sample_count]
    selected, held_out = set(selected_rows), set(inputs.split.validation_rows)
    training: dict[int, TokenizedTrainingExample] = {}
    validation: dict[int, TokenizedTrainingExample] = {}
    estimated_bytes = 0
    row = 0
    with inputs.source.open("rb") as source:
        while encoded := source.readline(1024**2 + 2):
            row += 1
            if len(encoded.rstrip(b"\n")) > 1024**2 or row > len(inputs.split.row_content_sha256):
                raise TrainingValidationError("Source exceeds frozen row/byte bounds")
            try:
                example = normalize_row(
                    json.loads(encoded),
                    format=inputs.binding.format,
                    dataset_id=inputs.binding.dataset_id,
                    source_row=row,
                )
            except (ValueError, UnicodeError, RecursionError):
                raise TrainingValidationError(
                    "Source row violates the frozen dataset schema"
                ) from None
            if example.content_sha256() != inputs.split.row_content_sha256[row - 1]:
                raise TrainingConflict("Source semantic content differs from frozen split")
            if example.loss_policy != spec.data.loss_policy:
                raise TrainingConflict("Resolved loss policy differs from normalized supervision")
            if row in selected or row in held_out:
                rendered = render_example(
                    example,
                    tokenizer,
                    max_sequence_length=spec.optim.max_sequence_length,
                    enable_thinking=prepared.resolved.enable_thinking,
                )
                estimated_bytes += len(rendered.tokens) * 48 + 1024
                if buffer_observer is not None:
                    buffer_observer(estimated_bytes)
                if estimated_bytes > prepared.resolved.resource_envelope.buffer_bytes:
                    raise TrainingValidationError(
                        "Tokenized source cache exceeds its accounted buffer envelope"
                    )
                (training if row in selected else validation)[row] = rendered
    if row != len(inputs.split.row_content_sha256):
        raise TrainingConflict("Source row count differs from frozen split")
    seed = spec.data.train.seed
    return (
        SingleSourceSampler(
            [training[index] for index in selected_rows],
            dataset_sha256=hashlib.sha256(
                (inputs.binding.split_sha256 + ":train:" + str(selected_rows)).encode()
            ).hexdigest(),
            batch_size=spec.optim.batch_size,
            seed=seed,
            max_sequence_length=spec.optim.max_sequence_length,
        ),
        SingleSourceSampler(
            [validation[index] for index in sorted(held_out)],
            dataset_sha256=hashlib.sha256(
                (inputs.binding.split_sha256 + ":validation").encode()
            ).hexdigest(),
            batch_size=1,
            seed=spec.data.validation.seed,
            max_sequence_length=spec.optim.max_sequence_length,
        ),
    )


def compile_samples(
    prepared: TrainingPrepareRequest,
    inputs: FrozenInputs | Sequence[FrozenInputs],
    tokenizer: ChatTokenizer,
    *,
    buffer_observer: Callable[[int], None] | None = None,
) -> tuple[TrainingSampler, TrainingSampler]:
    """Validate every frozen revision and build independently seeded train/held-out views.

    Preserve the original complete single-source algorithm and checkpoint identity for
    compatible jobs. All other intents use the shared strict mixture checkpoint state.
    """
    sources = [inputs] if isinstance(inputs, FrozenInputs) else list(inputs)
    by_id = {item.binding.dataset_id: item for item in sources}
    expected_ids = {item.dataset_id for item in prepared.resolved.spec.data.train.datasets} | set(
        prepared.resolved.spec.data.validation.dataset_ids
    )
    if (
        len(by_id) != len(sources)
        or set(by_id) != {selected.dataset_id for selected in prepared.resolved.datasets}
        or set(by_id) != expected_ids
        or len(prepared.resolved.datasets) != len(by_id)
    ):
        raise TrainingConflict("Frozen inputs must cover the exact resolved revision set")
    for source in sources:
        validate_frozen_inputs(prepared, source)
    if len({item.binding.model_slug for item in sources}) != 1:
        raise TrainingConflict("Frozen sources disagree on acquired model identity")
    spec = prepared.resolved.spec
    mixture = compile_mixture(
        spec.data.train,
        {key: item.split for key, item in by_id.items()},
        validation_manifests=[by_id[key].split for key in spec.data.validation.dataset_ids],
    )
    if (
        len(sources) == 1
        and len(mixture.sources) == 1
        and not mixture.replacement
        and mixture.strategy == "weighted"
        and mixture.epoch_samples == len(mixture.sources[0].rows)
        and mixture.epoch_samples % spec.optim.batch_size == 0
        and prepared.world_size == 1
    ):
        return _compile_single_samples(
            prepared, sources[0], tokenizer, buffer_observer=buffer_observer
        )
    selected_rows = {item.dataset_id: set(item.rows) for item in mixture.sources}
    validation_ids = set(spec.data.validation.dataset_ids)
    caches: dict[uuid.UUID, dict[int, TokenizedTrainingExample]] = {}
    estimated_bytes = (
        mixture.epoch_samples + sum(len(by_id[key].split.validation_rows) for key in validation_ids)
    ) * 256
    if buffer_observer is not None:
        buffer_observer(estimated_bytes)
    if estimated_bytes > prepared.resolved.resource_envelope.buffer_bytes:
        raise TrainingValidationError("Mixture indices exceed accounted buffer envelope")
    with tracer.start_as_current_span("coire.node.training.mixture.compile"):
        for source in sources:
            cache: dict[int, TokenizedTrainingExample] = {}
            source_id = source.binding.dataset_id
            needed = selected_rows.get(source_id, set()) | (
                set(source.split.validation_rows) if source_id in validation_ids else set()
            )
            verify_source(source.source, source.binding.source_sha256)
            row = 0
            with source.source.open("rb") as stream:
                while encoded := stream.readline(1024**2 + 2):
                    row += 1
                    if len(encoded.rstrip(b"\n")) > 1024**2 or row > len(
                        source.split.row_content_sha256
                    ):
                        raise TrainingValidationError("Source exceeds frozen row/byte bounds")
                    try:
                        example = normalize_row(
                            json.loads(encoded),
                            format=source.binding.format,
                            dataset_id=source_id,
                            source_row=row,
                        )
                    except (ValueError, UnicodeError, RecursionError):
                        raise TrainingValidationError("Source row violates frozen schema") from None
                    if example.content_sha256() != source.split.row_content_sha256[row - 1]:
                        raise TrainingConflict("Source semantic content differs from frozen split")
                    if example.loss_policy != spec.data.loss_policy:
                        raise TrainingConflict(
                            "Resolved loss policy differs from normalized supervision"
                        )
                    if row in needed:
                        rendered = render_example(
                            example,
                            tokenizer,
                            max_sequence_length=spec.optim.max_sequence_length,
                            enable_thinking=prepared.resolved.enable_thinking,
                        )
                        # Cache + defensive sampler copies + transient serialization/index
                        # space must all fit the aggregate accounted buffer, not per-source caps.
                        estimated_bytes += len(rendered.tokens) * 192 + 4096
                        if buffer_observer is not None:
                            buffer_observer(estimated_bytes)
                        if estimated_bytes > prepared.resolved.resource_envelope.buffer_bytes:
                            raise TrainingValidationError(
                                "Mixture cache exceeds accounted buffer envelope"
                            )
                        cache[row] = rendered
            if row != len(source.split.row_content_sha256):
                raise TrainingConflict("Source row count differs from frozen split")
            caches[source_id] = cache
        training_sources = [
            index_training_source(
                dataset_id=item.dataset_id,
                source_sha256=item.source_sha256,
                split_sha256=item.split_sha256,
                rows=item.rows,
                cache=caches[item.dataset_id],
                quota=item.quota,
            )
            for item in mixture.sources
        ]
        validation_sources = [
            index_training_source(
                dataset_id=key,
                source_sha256=by_id[key].binding.source_sha256,
                split_sha256=by_id[key].binding.split_sha256,
                rows=tuple(sorted(by_id[key].split.validation_rows)),
                cache=caches[key],
                quota=len(by_id[key].split.validation_rows),
            )
            for key in spec.data.validation.dataset_ids
        ]
        validation_identity = hashlib.sha256(
            json.dumps(
                [
                    spec.data.validation.model_dump(mode="json"),
                    [source.split_sha256 for source in validation_sources],
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return (
            MixtureSampler(
                training_sources,
                mixture_sha256=mixture.identity_sha256,
                batch_size=spec.optim.batch_size,
                seed=mixture.seed,
                max_sequence_length=spec.optim.max_sequence_length,
                strategy=mixture.strategy,
                replacement=mixture.replacement,
                rank=prepared.rank,
                world_size=prepared.world_size,
            ),
            MixtureSampler(
                validation_sources,
                mixture_sha256=validation_identity,
                batch_size=prepared.world_size,
                seed=spec.data.validation.seed,
                max_sequence_length=spec.optim.max_sequence_length,
                strategy="sequential",
                rank=prepared.rank,
                world_size=prepared.world_size,
            ),
        )


class PausedAtCheckpoint(Exception):
    """Upstream propagates this callback unwind after a committed full update."""


def restore_for_attempt(
    prepared: TrainingPrepareRequest,
    runtime: SftRuntime,
    optimizer: Any,
    sampler: TrainingSampler,
    store: CheckpointStore,
) -> int:
    """Only the controller-selected complete immutable checkpoint may initialize resume."""
    if prepared.resume_checkpoint_id is None:
        if int(optimizer.step.item()) != 0:
            raise TrainingConflict("Step-zero restart requires a fresh optimizer")
        return 0
    manifest = store.manifest(prepared.resume_checkpoint_id)
    if (
        manifest.canonical_sha256() != prepared.resume_manifest_sha256
        or manifest.job_id != prepared.job_id
        or manifest.world_size != prepared.world_size
    ):
        raise TrainingConflict("Resume manifest differs from selected checkpoint lineage")
    restored = store.restore(
        prepared.resume_checkpoint_id,
        expected_runtime_sha256=prepared.resolved.runtime_sha256,
        expected_resolved_spec_sha256=resolved_digest(prepared),
        rank=prepared.rank,
        expected_world_size=prepared.world_size,
    )
    apply_restored_checkpoint(
        restored, runtime, optimizer, sampler, expected_optimizer=prepared.resolved.spec.optim
    )
    return restored.state.completed_update


def finite_loss(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise TrainingValidationError("Trainer reported a nonfinite or invalid loss")
    return float(value)


def learning_rate(settings: TrainingOptimizer, step: Any, mx: Any) -> Any:
    if settings.schedule.kind == "constant":
        return settings.learning_rate
    warmup = settings.schedule.warmup_updates
    decay = mx.maximum(0, (settings.updates - step) / max(1, settings.updates - warmup))
    if warmup:
        return settings.learning_rate * mx.where(step < warmup, (step + 1) / warmup, decay)
    return settings.learning_rate * decay


def make_optimizer(settings: TrainingOptimizer) -> Any:
    import mlx.core as mx
    import mlx.optimizers as optim

    rate: Any = settings.learning_rate
    if settings.schedule.kind != "constant":

        def schedule(step: Any) -> Any:
            return learning_rate(settings, step, mx)

        rate = schedule
    kwargs = {
        "learning_rate": rate,
        "betas": [settings.beta1, settings.beta2],
        "eps": settings.epsilon,
    }
    if settings.name == "adamw":
        return optim.AdamW(**kwargs, weight_decay=settings.weight_decay)
    return optim.Adam(**kwargs)


def partition_batch(batch: Any, *, rank: int, world_size: int) -> tuple[Any, Any]:
    """All ranks advance the same global sampler; only the deterministic slice is used."""
    if world_size not in (1, 2) or not 0 <= rank < world_size or len(batch.tokens) % world_size:
        raise TrainingValidationError("Global training batch cannot be partitioned across ranks")
    width = len(batch.tokens) // world_size
    start = rank * width
    return batch.tokens[start : start + width], batch.target_masks[start : start + width]


class PrivateControl:
    """Owner-only filesystem mailbox; only strict scoped core commands are accepted.

    Credentials and control data never travel on a network listener or engine port.
    The generated spawn marker binds the private directory to this owned execution.
    """

    def __init__(self, directory: Path, prepared: TrainingPrepareRequest, owner: str) -> None:
        if (
            directory.is_symlink()
            or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o077
        ):
            raise TrainingValidationError("Worker control directory must be private and owned")
        self.directory = directory
        self.prepared = prepared
        self.lock = threading.RLock()
        self.commands: dict[str, str] = {}
        start = TrainingStartRequest.model_validate_json(self.read("lease.json"))
        if str(start.spawn_nonce) != owner or start.prepared_command_id != prepared.command_id:
            raise TrainingConflict("Worker ownership marker differs from durable spawn intent")
        self.scope(start)
        self.guard = ExecutionGuard(start.lease_expires_at)
        self.last_renewal: str | None = None
        self.pause_reason: Literal[
            "admin_pause", "latency_breach", "thermal_breach", "memory_breach", "lease_expired"
        ] = "admin_pause"

    def read(self, name: str) -> bytes:
        maximum = 1024 * 1024
        if name.startswith("collection-") and name.endswith(".json"):
            uuid.UUID(name.removeprefix("collection-").removesuffix(".json"))
            maximum = 64 * 1024**2
        fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > maximum
            ):
                raise TrainingValidationError("Private worker control file is unsafe")
            return os.read(fd, maximum + 1)
        finally:
            os.close(fd)

    def scope(self, command: Any) -> None:
        if any(
            getattr(command, field) != getattr(self.prepared, field)
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
            raise TrainingConflict("Worker control command differs from execution scope")
        identity, digest = str(command.command_id), command_digest(command)
        if identity in self.commands and self.commands[identity] != digest:
            raise TrainingConflict("Private control command changed after acceptance")
        if len(self.commands) >= 30_000 and identity not in self.commands:
            raise TrainingConflict("Worker control command bound exhausted")
        self.commands[identity] = digest

    def poll(self) -> str:
        with self.lock:
            return self._poll()

    def _poll(self) -> str:
        try:
            renewal = TrainingLeaseRenewal.model_validate_json(self.read("renew.json"))
            self.scope(renewal)
            if str(renewal.command_id) != self.last_renewal:
                self.guard.renew(renewal.lease_expires_at)
                self.last_renewal = str(renewal.command_id)
        except FileNotFoundError:
            pass
        try:
            pause = TrainingPauseRequest.model_validate_json(self.read("pause.json"))
            self.scope(pause)
            self.pause_reason = pause.reason
            self.guard.pause()
        except FileNotFoundError:
            pass
        return self.guard.action()

    def watchdog(self, stopped: threading.Event, *, terminate: Callable[[], None]) -> None:
        while not stopped.wait(0.5):
            try:
                if self.poll() == "kill":
                    terminate()
                    return
            except Exception:
                # Malformed, stale or changed authority never becomes infinite execution.
                terminate()
                return


def run_sft(
    prepared: TrainingPrepareRequest,
    runtime: SftRuntime,
    optimizer: Any,
    train_sampler: TrainingSampler,
    validation_sampler: TrainingSampler,
    *,
    store: CheckpointStore,
    scratch: Path,
    completed_update: int = 0,
    emit: Callable[[NodeTrainingEvent], None],
    commit: Callable[[TrainingArtifactManifest], CheckpointCommitAcknowledgement | None],
    control: Callable[[], str],
    footprint: Callable[[], int],
    checkpoint: Callable[[CheckpointWorkerState, dict[str, Any], Any], TrainingArtifactManifest]
    | None = None,
    measurement_checkpoint: Callable[[CheckpointWorkerState, dict[str, Any], Any], None]
    | None = None,
    collective: Collective | None = None,
    guardian: DeadlineGuardian | None = None,
) -> int:
    """Train only evaluated complete updates; block advancement until fenced commit.

    `emit` must persist before returning. `commit` must verify all copies through the
    controller and obey the remaining lease/pause deadline. A local save isn't durable.
    """
    if platform.node().lower().split(".", 1)[0] == "coire-core":
        raise TrainingValidationError("Training is forbidden on core")
    if measurement_checkpoint is not None and (checkpoint is not None or completed_update != 0):
        raise TrainingValidationError("Measurement cannot mix durable checkpoints or resume state")
    if prepared.world_size != 1 and checkpoint is None and measurement_checkpoint is None:
        raise TrainingValidationError("Common-rank full-state checkpoint coordination unavailable")
    if (
        train_sampler is validation_sampler
        or train_sampler.dataset_sha256 == validation_sampler.dataset_sha256
    ):
        raise TrainingValidationError("Held-out evaluation requires an independent split sampler")
    settings = prepared.resolved.spec.optim
    if not 0 <= completed_update < settings.updates:
        raise TrainingConflict("Resume update lies outside remaining training")
    if train_sampler.batch_size != settings.batch_size:
        raise TrainingConflict("Sampler batch differs from resolved optimizer")
    import mlx.core as mx
    from mlx_lm.tuner.callbacks import TrainingCallback
    from mlx_lm.tuner.trainer import TrainingArgs, evaluate, train

    if prepared.world_size == 2:
        if collective is None or guardian is None:
            raise TrainingValidationError("Two-rank training requires a guarded bare collective")
        from coire_node.training.checkpoints import MlxTensorIO

        mx.eval(runtime.model.state)
        collective.compare(parameter_digest(runtime.parameters(), MlxTensorIO()))
        guardian.progress()

    if int(optimizer.step.item()) != completed_update:
        raise TrainingConflict("Optimizer step differs from resumed completed update")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    if scratch.is_symlink():
        raise TrainingValidationError("Scratch adapter directory is linked")
    sequence = 0
    current_update = completed_update

    def event(payload: Any) -> None:
        nonlocal sequence
        sequence += 1
        emit(
            NodeTrainingEvent(
                sequence=sequence,
                job_id=prepared.job_id,
                attempt_id=prepared.attempt_id,
                fence=prepared.fence,
                update=current_update,
                recorded_at=datetime.now(UTC),
                payload=payload,
            )
        )

    def batches(sampler: TrainingSampler) -> Callable[..., Iterator[tuple[Any, Any]]]:
        def iterate(**kwargs: Any) -> Iterator[tuple[Any, Any]]:
            if kwargs["comm_group"].size() != prepared.world_size:
                raise TrainingConflict("Bare trainer group differs from declared world size")
            if kwargs["comm_group"].rank() != prepared.rank:
                raise TrainingConflict("Bare trainer group differs from owned rank")
            while True:
                if control() in {"kill", "cancel"}:
                    raise TrainingConflict("Training execution authority ended")
                batch = sampler.next_batch()
                if isinstance(sampler, MixtureSampler):
                    if sampler.rank != prepared.rank or sampler.world_size != prepared.world_size:
                        raise TrainingConflict("Mixture sampler differs from declared rank")
                    tokens, masks = batch.tokens, batch.target_masks
                else:
                    tokens, masks = partition_batch(
                        batch, rank=prepared.rank, world_size=prepared.world_size
                    )
                yield mx.array(tokens, dtype=mx.int32), mx.array(masks, dtype=mx.bool_)

        return iterate

    def report(kind: Literal["train", "validation"], loss: float, info: dict[str, Any]) -> None:
        event(
            NodeProgressPayload(
                metric=TrainingMetricSample(
                    job_id=prepared.job_id,
                    attempt_id=prepared.attempt_id,
                    update=current_update,
                    kind=kind,
                    loss=finite_loss(loss),
                    learning_rate=float(optimizer.learning_rate.item()),
                    tokens=int(info.get("trained_tokens", 0)),
                    tokens_per_second=float(info.get("tokens_per_second", 0)),
                    updates_per_second=float(info.get("iterations_per_second", 0))
                    / settings.accumulation_steps,
                    footprint_bytes=footprint(),
                    peak_bytes=int(mx.get_peak_memory()),
                    recorded_at=datetime.now(UTC),
                )
            )
        )

    class Callback(TrainingCallback):
        def on_train_loss_report(self, info: dict[str, Any]) -> None:
            nonlocal current_update
            iteration = info["iteration"]
            if iteration % settings.accumulation_steps:
                raise TrainingValidationError("Trainer callback is not a completed-update boundary")
            current_update = completed_update + iteration // settings.accumulation_steps
            if int(optimizer.step.item()) != current_update:
                raise TrainingValidationError("Optimizer counter differs from completed update")
            if guardian is not None:
                guardian.progress()
            report("train", finite_loss(info["train_loss"]), info)
            updates.add(1, {"node": prepared.node})
            logger.info(
                "training update persisted",
                extra={
                    "job_id": prepared.job_id,
                    "attempt_id": prepared.attempt_id,
                    "model_id": str(prepared.resolved.spec.model.model_id),
                    "update": current_update,
                },
            )
            spec = prepared.resolved.spec
            final = current_update == settings.updates
            if current_update % spec.eval.loss_every_updates == 0 or (final and spec.eval.at_end):
                key = capture_mlx_rng_key()
                validation_state = validation_sampler.snapshot()
                try:
                    loss = evaluate(
                        runtime.model,
                        validation_sampler.dataset,
                        batch_size=validation_sampler.batch_size,
                        num_batches=min(
                            len(validation_sampler.dataset) // validation_sampler.batch_size,
                            prepared.resolved.spec.data.validation.max_batches,
                        ),
                        max_seq_length=settings.max_sequence_length,
                        loss=masked_sft_loss,
                        iterate_batches=batches(validation_sampler),
                    )
                    report("validation", finite_loss(loss), {})
                finally:
                    validation_sampler.restore(validation_state)
                    restore_mlx_rng_key(key)
                    runtime.model.train()
            action = collective.action(control()) if collective is not None else control()
            if action in {"kill", "cancel"}:
                raise TrainingConflict("Training execution authority ended")
            if (
                current_update % spec.output.checkpoint_every_updates == 0
                or final
                or action == "pause"
            ):
                getter = cast(Callable[[], dict[str, Any]], runtime.model.trainable_parameters)
                mx.eval(getter(), optimizer.state)
                state = CheckpointWorkerState(
                    job_id=prepared.job_id,
                    attempt_id=prepared.attempt_id,
                    fence=prepared.fence,
                    completed_update=current_update,
                    rank=prepared.rank,
                    world_size=prepared.world_size,
                    runtime_sha256=prepared.resolved.runtime_sha256,
                    resolved_spec_sha256=resolved_digest(prepared),
                    optimizer=settings,
                    mlx_rng_key=capture_mlx_rng_key(),
                    sampler=train_sampler.snapshot(),
                )
                adapter = {
                    key: value
                    for key, value in runtime.parameters().items()
                    if key in runtime.trainable_keys
                }
                if measurement_checkpoint is not None:
                    # Measure actual evaluated per-rank state without manufacturing
                    # a durable common bundle, staged event or commit acknowledgment.
                    # Only the supervised measurement lane supplies this internal hook.
                    measurement_checkpoint(state, adapter, optimizer.state)
                    action = collective.action(control()) if collective is not None else control()
                    if action in {"pause", "kill", "cancel"}:
                        raise TrainingConflict("Non-durable measurement checkpoint authority ended")
                    return
                manifest = (
                    checkpoint(state, adapter, optimizer.state)
                    if checkpoint is not None
                    else store.save(state, adapter, optimizer.state)
                )
                manifest = TrainingArtifactManifest.model_validate(manifest.model_dump(mode="json"))
                if (
                    manifest.kind != "checkpoint"
                    or manifest.world_size != prepared.world_size
                    or manifest.job_id != prepared.job_id
                    or manifest.attempt_id != prepared.attempt_id
                    or manifest.fence != prepared.fence
                    or manifest.update != current_update
                    or manifest.runtime_sha256 != state.runtime_sha256
                    or manifest.resolved_spec_sha256 != state.resolved_spec_sha256
                ):
                    raise TrainingConflict(
                        "Checkpoint collector returned a mismatched common bundle"
                    )
                event(NodeCheckpointPayload(manifest=manifest))
                acknowledgement = commit(manifest)
                if acknowledgement is not None:
                    acknowledgement = CheckpointCommitAcknowledgement.model_validate(
                        acknowledgement.model_dump(mode="json")
                    )
                    valid_ack = (
                        acknowledgement.checkpoint_id == manifest.artifact_id
                        and acknowledgement.manifest_sha256 == manifest.canonical_sha256()
                        and acknowledgement.update == current_update
                        and 0
                        < (acknowledgement.lease_expires_at - datetime.now(UTC)).total_seconds()
                        <= 30
                        and all(
                            getattr(acknowledgement, field) == getattr(prepared, field)
                            for field in (
                                "job_id",
                                "attempt_id",
                                "fence",
                                "node",
                                "rank",
                                "world_size",
                            )
                        )
                    )
                else:
                    valid_ack = False
                if not valid_ack or control() in {"kill", "cancel"}:
                    raise TrainingConflict(
                        "Checkpoint has no current fenced durable acknowledgement"
                    )
                # Every rank must finish the SAME commit before callback unwind; a
                # pause arriving on one rank must not leave its peer in the next step.
                action = collective.action(control()) if collective is not None else control()
                if action in {"kill", "cancel"}:
                    raise TrainingConflict("Rank authority ended after common checkpoint commit")
                if action == "pause":
                    raise PausedAtCheckpoint()

    accumulation = settings.accumulation_steps
    remaining = (settings.updates - completed_update) * accumulation
    args = TrainingArgs(
        iters=remaining,
        batch_size=settings.batch_size,
        grad_accumulation_steps=accumulation,
        steps_per_report=accumulation,
        steps_per_save=remaining + 1,
        max_seq_length=settings.max_sequence_length,
        adapter_file=str(scratch / "upstream-scratch.safetensors"),
        grad_checkpoint=False,
    )
    with tracer.start_as_current_span(
        "coire.node.training.execute",
        attributes={
            "job_id": prepared.job_id,
            "attempt_id": prepared.attempt_id,
            "node": prepared.node,
        },
    ):
        train(
            runtime.model,
            optimizer,
            train_sampler.dataset,
            args=args,
            loss=masked_sft_loss,
            iterate_batches=batches(train_sampler),
            training_callback=Callback(),
        )
    return current_update


def resolved_digest(prepared: TrainingPrepareRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            prepared.resolved.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def wait_checkpoint_commit(
    channel: PrivateControl, manifest: TrainingArtifactManifest
) -> CheckpointCommitAcknowledgement | None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if channel.poll() in {"cancel", "kill"}:
            return None
        try:
            result = CheckpointCommitAcknowledgement.model_validate_json(
                channel.read(f"commit-{manifest.artifact_id}.json")
            )
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        channel.scope(result)
        if (
            result.checkpoint_id != manifest.artifact_id
            or result.manifest_sha256 != manifest.canonical_sha256()
            or result.update != manifest.update
        ):
            raise TrainingConflict("Private checkpoint acknowledgement differs from staged bytes")
        return result
    return None


def execute_native(
    journal: TrainingJournal,
    prepared: TrainingPrepareRequest,
    *,
    owner: str,
    store_root: Path,
    artifact_root: Path,
    rank_transport: RankTransport | None = None,
) -> int:
    """Fixed native bootstrap; every executable input comes from the node-owned journal."""
    if (
        platform.node().lower().split(".", 1)[0] == "coire-core"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
    ):
        raise TrainingValidationError("Native training requires a non-core Apple Silicon worker")
    value = journal.get(prepared.attempt_id)
    if (
        command_digest(TrainingPrepareRequest.model_validate(value["prepare"]))
        != command_digest(prepared)
        or value["spawn_nonce"] != owner
        or value["released"]
        or value["liveness"] == "stopped"
    ):
        raise TrainingConflict("Worker differs from durable owned spawn intent")
    if os.getpgrp() != os.getpid():
        raise TrainingConflict("Native worker must own its separate process group")
    if prepared.resolved.worker_version != "1":
        raise TrainingConflict("Native worker version differs from pinned execution")
    directory = journal.root / prepared.attempt_id
    channel = PrivateControl(directory, prepared, owner)
    import psutil

    birth = TrainingStartReceipt(
        attempt_id=prepared.attempt_id,
        fence=prepared.fence,
        pid=os.getpid(),
        process_create_time=psutil.Process().create_time(),
        reservation_id=prepared.reservation_id,
    )
    if value["pid"] is not None and (
        value["pid"] != birth.pid or value["process_create_time"] != birth.process_create_time
    ):
        raise TrainingConflict("Native process differs from recorded PID/create-time")
    try:
        previous_birth = TrainingStartReceipt.model_validate_json(
            read_private(directory / "birth.json", 1024**2)
        )
        if previous_birth != birth:
            raise TrainingConflict("Durable worker birth marker is immutable")
    except FileNotFoundError:
        pass
    write_atomic(directory / "birth.json", birth.model_dump_json().encode())
    fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    stopped = threading.Event()
    began = time.monotonic()
    guardian = DeadlineGuardian() if prepared.world_size == 2 else None

    def footprint() -> int:
        value = phys_footprint(os.getpid())
        if value is None:
            raise TrainingValidationError("Native training footprint is unavailable")
        return value

    def watchdog() -> None:
        while not stopped.wait(0.5):
            try:
                if (
                    channel.poll() == "kill"
                    or footprint() > prepared.resolved.resource_envelope.memory_bytes
                    or time.monotonic() - began >= 72 * 3600
                    or (guardian is not None and guardian.expired())
                ):
                    os._exit(124)
            except Exception:
                os._exit(124)

    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    failure_reason: TrainingReason = "invalid_input"
    try:
        if prepared.world_size == 2 and prepared.collective is None:
            raise TrainingValidationError("Native rank requires its frozen collective binding")
        if prepared.world_size == 2 and rank_transport is None:
            from coire_node.training.components import TrainingComponents

            rank_transport = PrivateRankTransport(
                prepared,
                TrainingComponents(artifact_root, journal),
                journal,
                read=channel.read,
                control=channel.poll,
                scope=channel.scope,
            )
        all_inputs = load_all_frozen_inputs(prepared, directory, journal)
        inputs = all_inputs[0]
        store = Store(store_root)
        model_path = store.path_for(inputs.binding.model_slug)
        failure_reason = "runtime_mismatch"
        tokenizer, tokenizer_sha, template_sha, runtime_sha = load_analysis_tokenizer(
            model_path, inputs.binding
        )
        if (
            tokenizer_sha != prepared.resolved.tokenizer_sha256
            or template_sha != prepared.resolved.template_sha256
            or runtime_sha != prepared.resolved.runtime_sha256
        ):
            raise TrainingConflict(
                "Native tokenizer/template/runtime differs from resolved execution"
            )
        failure_reason = "invalid_input"
        training, validation = compile_samples(prepared, all_inputs, tokenizer)
        if channel.poll() in {"kill", "cancel"}:
            raise TrainingConflict("Execution authority expired before model loading")
        failure_reason = "runtime_mismatch"
        source = validate_sft_input(
            model_path,
            prepared.resolved.spec.parameterization,
            expected_manifest_sha256=prepared.resolved.base_manifest_sha256,
        )
        with tracer.start_as_current_span("coire.node.training.load"):
            runtime = load_sft_runtime(source, seed=prepared.resolved.spec.seed)
        if inputs.binding.template_override is not None:
            runtime.tokenizer.chat_template = inputs.binding.template_override
        optimizer = make_optimizer(prepared.resolved.spec.optim)
        checkpoints = CheckpointStore(artifact_root, max_bytes=20 * 1024**3)
        failure_reason = "checkpoint_invalid"
        offset = restore_for_attempt(prepared, runtime, optimizer, training, checkpoints)
        collective = BareCollective(prepared) if prepared.world_size == 2 else None
        coordinator = (
            RankCheckpointCoordinator(
                prepared, checkpoints, rank_transport, collective, control=channel.poll
            )
            if rank_transport is not None and collective is not None
            else None
        )
        failure_reason = "internal"
        run_sft(
            prepared,
            runtime,
            optimizer,
            training,
            validation,
            store=checkpoints,
            scratch=directory / "scratch",
            completed_update=offset,
            emit=lambda event: journal.append_event(
                event.model_copy(update={"sequence": journal.next_sequence(prepared.attempt_id)})
            ),
            commit=lambda manifest: wait_checkpoint_commit(channel, manifest),
            control=channel.poll,
            footprint=footprint,
            checkpoint=coordinator.checkpoint if coordinator is not None else None,
            collective=collective,
            guardian=guardian,
        )
        return 0
    except PausedAtCheckpoint:
        journal.append_event(
            NodeTrainingEvent(
                sequence=journal.next_sequence(prepared.attempt_id),
                job_id=prepared.job_id,
                attempt_id=prepared.attempt_id,
                fence=prepared.fence,
                update=journal.get(prepared.attempt_id).get("update", 0),
                recorded_at=datetime.now(UTC),
                payload=NodeControlPayload(kind="stopped", reason=channel.pause_reason),
            )
        )
        return 0
    except Exception:
        # Expired/fenced authority cannot publish even a late failure event.
        with suppress(TrainingConflict, TrainingValidationError):
            journal.append_event(
                NodeTrainingEvent(
                    sequence=journal.next_sequence(prepared.attempt_id),
                    job_id=prepared.job_id,
                    attempt_id=prepared.attempt_id,
                    fence=prepared.fence,
                    update=journal.get(prepared.attempt_id).get("update", 0),
                    recorded_at=datetime.now(UTC),
                    payload=NodeControlPayload(kind="failure", reason=failure_reason),
                )
            )
        logger.error(
            "native training failed",
            extra={
                "job_id": prepared.job_id,
                "attempt_id": prepared.attempt_id,
                "reason": failure_reason,
            },
        )
        return 1
    finally:
        stopped.set()
        watcher.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--attempt",
        required=True,
        type=lambda value: TypeAdapter(TrainingId).validate_python(value),
    )
    parser.add_argument("--owner", required=True, type=uuid.UUID)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    args = parser.parse_args()
    if not all(
        path.is_absolute() for path in (args.state_root, args.store_root, args.artifact_root)
    ):
        parser.error("native worker roots must be absolute node configuration")
    if (
        platform.node().lower().split(".", 1)[0] == "coire-core"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
    ):
        parser.error("native training requires a non-core Apple Silicon worker")
    prepared = TrainingPrepareRequest.model_validate_json(
        read_private(args.state_root / args.attempt / "prepare.json", 1024**2)
    )
    if prepared.attempt_id != args.attempt:
        parser.error("native attempt differs from node-owned preparation")
    from coire_node.training.telemetry import initialize_training_telemetry

    initialize_training_telemetry()
    journal = TrainingJournal(args.state_root, node=prepared.node, admission_lock=threading.RLock())
    try:
        with tracer.start_as_current_span(
            "coire.node.training.worker",
            attributes={
                "job_id": prepared.job_id,
                "attempt_id": prepared.attempt_id,
                "node": prepared.node,
            },
        ):
            return execute_native(
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
