"""Exact local initial policies and independent frozen DPO references.

Only node-owned native execution outside core calls the loader. Parent artifacts
are immutable local registry targets; no Hub identifiers or caller paths enter
this module through a wire contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from coire_core.errors import TrainingValidationError
from coire_core.models.adapters import InferenceTarget
from coire_core.models.preference import DpoOptions, OrpoOptions
from coire_core.models.training import TrainingParameterization
from coire_core.models.training_node import TrainingArtifactManifest
from coire_node.training.objectives import (
    SftInput,
    SftRuntime,
    load_sft_runtime,
    validate_sft_input,
)

if TYPE_CHECKING:
    from coire_node.training.checkpoints import RestoredCheckpoint
    from coire_node.training.preference_data import PreferenceSampler


@dataclass
class PreferenceRuntime:
    policy: SftRuntime
    reference: SftRuntime | None
    initial_target: InferenceTarget
    objective: Literal["dpo", "orpo"]


def validate_preference_input(
    root: Path, parameters: TrainingParameterization, *, expected_manifest_sha256: str | None = None
) -> SftInput:
    if parameters.kind not in {"lora", "qlora"} or parameters.dropout != 0:
        raise TrainingValidationError("Preference execution requires zero-dropout LoRA or QLoRA")
    return validate_sft_input(root, parameters, expected_manifest_sha256=expected_manifest_sha256)


def validate_initial_adapter(
    source: SftInput, target: InferenceTarget, directory: Path | None
) -> Path | None:
    if source.manifest.sha256() != target.base_manifest_sha256:
        raise TrainingValidationError("Initial policy base differs from its acquired manifest")
    if target.adapter_id is None:
        if directory is not None:
            raise TrainingValidationError("Bare initialization cannot load an adapter")
        return None
    if directory is None or not directory.is_absolute() or directory.name != str(target.adapter_id):
        raise TrainingValidationError("Initial adapter must be the exact local artifact identity")
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise TrainingValidationError("Initial adapter artifact path is linked")
    from coire_node.training.extraction import private_open, private_read

    try:
        manifest = TrainingArtifactManifest.model_validate_json(
            private_read(directory / "manifest.json", 1024**2)
        )
        if (
            manifest.artifact_id != target.adapter_id
            or manifest.kind != "adapter"
            or manifest.canonical_sha256() != target.adapter_manifest_sha256
            or {entry.name for entry in manifest.files}
            != {"adapter_config.json", "adapters.safetensors"}
        ):
            raise TrainingValidationError("Initial adapter immutable manifest differs")
        for entry in manifest.files:
            path = directory / entry.name
            # private_read also checks owner, mode, hard links, type and every parent.
            with private_open(path) as stream:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                if (
                    os.fstat(stream.fileno()).st_size != entry.bytes
                    or digest.hexdigest() != entry.sha256
                ):
                    raise TrainingValidationError("Initial adapter artifact checksum differs")
        config = json.loads(private_read(directory / "adapter_config.json", 64 * 1024))
        p = source.parameterization
        if (
            not isinstance(config, dict)
            or config.get("fine_tune_type") != "lora"
            or config.get("num_layers") != p.num_layers
            or config.get("lora_parameters")
            != {"rank": p.rank, "scale": p.scale, "dropout": 0.0, "keys": p.target_modules}
            or config.get("coire_base_model_id") != str(target.model_id)
            or config.get("coire_base_variant_id") != str(target.variant_id)
            or config.get("coire_base_manifest_sha256") != target.base_manifest_sha256
        ):
            raise TrainingValidationError(
                "Initial adapter configuration differs from exact training parameters"
            )
        return directory / "adapters.safetensors"
    except TrainingValidationError:
        raise
    except (OSError, ValueError, RecursionError):
        raise TrainingValidationError(
            "Initial adapter artifact is invalid or unavailable"
        ) from None


def _load_initial_weights(runtime: SftRuntime, weights: Path | None) -> None:
    if weights is None:
        return
    import mlx.core as mx

    from coire_core.models.training_node import TensorDescriptor
    from coire_node.training.checkpoints import _check_tensor_header

    current = runtime.parameters()
    expected = [
        TensorDescriptor(key=key, shape=list(current[key].shape), dtype="float32")
        for key in sorted(runtime.trainable_keys)
    ]
    _check_tensor_header(weights, expected)
    tensors = cast(dict[str, mx.array], mx.load(str(weights)))
    if set(tensors) != runtime.trainable_keys:
        raise TrainingValidationError("Initial adapter tensors differ from trainable policy keys")
    # Complete key/shape/dtype validation precedes the upstream subset update.
    runtime.model.load_weights(list(tensors.items()), strict=False)
    mx.eval(runtime.parameters())


def load_preference_runtime(
    source: SftInput,
    *,
    seed: int,
    objective: Literal["dpo", "orpo"],
    initial_target: InferenceTarget,
    initial_adapter: Path | None = None,
) -> PreferenceRuntime:
    if platform.node().lower().split(".", 1)[0] == "coire-core":
        raise TrainingValidationError("Preference model work is forbidden on core")
    if objective not in {"dpo", "orpo"}:
        raise TrainingValidationError("Preference objective is unsupported")
    validate_preference_input(
        source.root, source.parameterization, expected_manifest_sha256=source.manifest.sha256()
    )
    weights = validate_initial_adapter(source, initial_target, initial_adapter)
    from coire_node.training.checkpoints import capture_mlx_rng_key, restore_mlx_rng_key

    policy = load_sft_runtime(source, seed=seed)
    _load_initial_weights(policy, weights)
    reference = None
    if objective == "dpo":
        saved_rng = capture_mlx_rng_key()
        try:
            reference = load_sft_runtime(source, seed=seed)
            _load_initial_weights(reference, weights)
            reference.model.freeze()
            reference.model.eval()
            reference.trainable_keys = frozenset()
            reference.frozen_parameters = reference.parameters()
        finally:
            restore_mlx_rng_key(saved_rng)
    return PreferenceRuntime(policy, reference, initial_target, objective)


def apply_preference_checkpoint(
    restored: RestoredCheckpoint,
    runtime: PreferenceRuntime,
    optimizer: Any,
    sampler: PreferenceSampler,
    *,
    expected_optimizer: Any,
    expected_options: DpoOptions | OrpoOptions,
) -> None:
    from coire_core.models.training_node import CheckpointWorkerStateV3
    from coire_node.training.checkpoints import apply_restored_checkpoint

    state = restored.state
    if not isinstance(state, CheckpointWorkerStateV3):
        raise TrainingValidationError("Preference runtime cannot resume an SFT checkpoint")
    if (
        state.objective != runtime.objective
        or state.objective_options != expected_options
        or state.initial_target != runtime.initial_target
        or state.reference_target
        != (runtime.initial_target if runtime.objective == "dpo" else None)
        or (runtime.objective == "dpo") != (runtime.reference is not None)
        or (runtime.reference is not None and runtime.reference.model is runtime.policy.model)
    ):
        raise TrainingValidationError(
            "Preference checkpoint differs from the original policy/reference"
        )
    apply_restored_checkpoint(
        restored, runtime.policy, optimizer, sampler, expected_optimizer=expected_optimizer
    )
