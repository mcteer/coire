"""Isolated fresh-process native preference resume fixture; never a production entry point."""

from __future__ import annotations

import argparse
import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--objective", choices=("dpo", "orpo"), required=True)
    parser.add_argument(
        "--mode", choices=("prepare", "uninterrupted", "pause", "resume"), required=True
    )
    parser.add_argument("--parent", action="store_true")
    args = parser.parse_args()
    from coire_node.testing.training import offline_training_model

    if os.environ.get("COIRE_TRAINING_ENGINE") != "1":
        raise ValueError("native preference fixture requires explicit opt-in")
    root = offline_training_model(os.environ.get("COIRE_TEST_MODEL"))
    directory = args.directory.resolve(strict=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("native fixture requires its private owned test directory")
    import mlx.core as mx
    import mlx.optimizers as optim
    import numpy as np
    from mlx.nn.utils import value_and_grad
    from mlx.utils import tree_flatten
    from mlx_lm.tuner.callbacks import TrainingCallback
    from mlx_lm.tuner.trainer import TrainingArgs, train

    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.preference import (
        DpoOptions,
        OrpoOptions,
        TokenizedPreferenceExample,
        canonical_bytes,
    )
    from coire_core.models.training import TrainingOptimizer, TrainingParameterization
    from coire_core.models.training_node import (
        CheckpointWorkerStateV3,
        TrainingArtifactFile,
        TrainingArtifactManifest,
    )
    from coire_node.training.checkpoints import CheckpointStore, capture_mlx_rng_key
    from coire_node.training.loss import masked_sft_loss
    from coire_node.training.objectives import load_sft_runtime
    from coire_node.training.preference_data import IndexedPreferenceSource, PreferenceSampler
    from coire_node.training.preference_loss import make_preference_loss
    from coire_node.training.preference_runtime import (
        apply_preference_checkpoint,
        load_preference_runtime,
        validate_preference_input,
    )

    flatten = cast(Callable[[Any], list[tuple[str, Any]]], tree_flatten)
    kind = os.environ["COIRE_TRAINING_PARAMETERIZATION"]
    parameters = TrainingParameterization.model_validate(
        {"kind": kind, "rank": 2, "target_modules": ["self_attn.q_proj", "self_attn.v_proj"]}
    )
    source = validate_preference_input(root, parameters)
    initial = InferenceTarget(
        model_id=uuid.UUID(int=1),
        variant_id=uuid.UUID(int=2),
        base_manifest_sha256=source.manifest.sha256(),
    )
    parent_directory = directory / str(uuid.UUID(int=3))
    if args.mode == "prepare":
        if args.parent:
            parent = load_sft_runtime(source, seed=42)
            optimizer = optim.AdamW(learning_rate=1e-3)
            tokens = mx.array([[1, 2, 8, 9]], dtype=mx.int32)
            masks = mx.array([[False, True, True, True]], dtype=mx.bool_)

            def parent_loss(model: Any) -> Any:
                return masked_sft_loss(model, tokens, masks)[0]

            for _ in range(2):
                loss, gradients = value_and_grad(parent.model, parent_loss)(parent.model)
                optimizer.update(parent.model, gradients)
                mx.eval(parent.parameters(), optimizer.state, loss)
            parent_directory.mkdir(mode=0o700)
            weights = parent_directory / "adapters.safetensors"
            mx.save_safetensors(
                str(weights),
                {
                    key: value
                    for key, value in parent.parameters().items()
                    if key in parent.trainable_keys
                },
            )
            weights.chmod(0o600)
            config = parent_directory / "adapter_config.json"
            config.write_bytes(
                canonical_bytes(
                    {
                        "fine_tune_type": "lora",
                        "num_layers": parameters.num_layers,
                        "lora_parameters": {
                            "rank": parameters.rank,
                            "scale": parameters.scale,
                            "dropout": 0.0,
                            "keys": parameters.target_modules,
                        },
                        "coire_base_model_id": str(initial.model_id),
                        "coire_base_variant_id": str(initial.variant_id),
                        "coire_base_manifest_sha256": initial.base_manifest_sha256,
                    }
                )
            )
            config.chmod(0o600)
            entries = [
                TrainingArtifactFile(
                    id=path.stem,
                    name=path.name,
                    bytes=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in (weights, config)
            ]
            manifest = TrainingArtifactManifest(
                artifact_id=uuid.UUID(int=3),
                kind="adapter",
                files=entries,
                total_bytes=sum(entry.bytes for entry in entries),
            )
            path = parent_directory / "manifest.json"
            path.write_text(manifest.model_dump_json())
            path.chmod(0o600)
        return 0
    if args.parent:
        manifest = TrainingArtifactManifest.model_validate_json(
            (parent_directory / "manifest.json").read_bytes()
        )
        initial = initial.model_copy(
            update={
                "adapter_id": manifest.artifact_id,
                "adapter_manifest_sha256": manifest.canonical_sha256(),
            }
        )
    runtime = load_preference_runtime(
        source,
        seed=42,
        objective=args.objective,
        initial_target=initial,
        initial_adapter=parent_directory if args.parent else None,
    )
    settings = TrainingOptimizer(
        updates=16, batch_size=2, accumulation_steps=2, max_sequence_length=8, learning_rate=1e-3
    )
    options = DpoOptions(beta=0.1) if args.objective == "dpo" else OrpoOptions(weight=0.1)
    examples = [
        TokenizedPreferenceExample(
            source_row=index + 1,
            content_sha256=f"{index + 1:064x}",
            prompt_sha256=f"{index + 1:064x}",
            chosen_tokens=[1, 2 + index, 8, 9],
            rejected_tokens=[1, 3 + index, 9],
            chosen_mask=[False, True, True, True],
            rejected_mask=[False, True, True],
            prompt_length=1,
        )
        for index in range(4)
    ]
    sampler = PreferenceSampler(
        [
            IndexedPreferenceSource(
                dataset_id=uuid.UUID(int=4),
                split_sha256="a" * 64,
                rows=(1, 2, 3, 4),
                quota=4,
                examples=examples,
            )
        ],
        mixture_sha256="b" * 64,
        batch_size=2,
        seed=42,
        max_sequence_length=8,
    )
    optimizer = optim.AdamW(learning_rate=settings.learning_rate)
    store = CheckpointStore(directory / "checkpoints", disk_floor_bytes=0)
    runtime_digest = hashlib.sha256(b"mlx0.32.2:mlx-lm0.31.3:coire-preference-v1").hexdigest()
    resolved_digest = hashlib.sha256(
        canonical_bytes(
            [
                initial.model_dump(mode="json"),
                args.objective,
                options.model_dump(),
                settings.model_dump(),
                sampler.dataset_sha256,
            ]
        )
    ).hexdigest()
    offset = 0
    if args.mode == "resume":
        restored = store.restore(
            uuid.UUID(int=5),
            expected_runtime_sha256=runtime_digest,
            expected_resolved_spec_sha256=resolved_digest,
        )
        apply_preference_checkpoint(
            restored,
            runtime,
            optimizer,
            sampler,
            expected_optimizer=settings,
            expected_options=options,
        )
        offset = restored.state.completed_update

    def fingerprint() -> str | None:
        if runtime.reference is None:
            return None
        digest = hashlib.sha256()
        for key in sorted(runtime.policy.trainable_keys):
            digest.update(key.encode())
            digest.update(np.array(runtime.reference.parameters()[key]).tobytes())
        probe = runtime.reference.model(mx.array([[1, 2, 8]], dtype=mx.int32))[:, :, :16]
        mx.eval(probe)
        digest.update(np.array(probe.astype(mx.float32)).tobytes())
        return digest.hexdigest()

    reference_before = fingerprint()
    observations: dict[str, Any] = {
        "updates": [],
        "losses": [],
        "rng": [],
        "sampler": [],
        "reference_sha256": reference_before,
    }
    arrays: dict[str, Any] = {}

    class Paused(Exception):
        pass

    class Callback(TrainingCallback):
        def on_train_loss_report(self, info: dict[str, Any]) -> None:
            update = offset + info["iteration"] // settings.accumulation_steps
            assert optimizer.step.item() == update
            assert fingerprint() == reference_before
            if update > 8:
                observations["updates"].append(update)
                observations["losses"].append(float(info["train_loss"]))
                observations["rng"].append(capture_mlx_rng_key())
                observations["sampler"].append(sampler.snapshot().model_dump(mode="json"))
                for key in sorted(runtime.policy.trainable_keys):
                    arrays[f"policy:{update}:{key}"] = np.array(runtime.policy.parameters()[key])
                for key, value in flatten(optimizer.state):
                    arrays[f"optimizer:{update}:{key}"] = np.array(value)
            if args.mode == "pause" and update == 8:
                state = CheckpointWorkerStateV3(
                    job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    fence=1,
                    completed_update=update,
                    runtime_sha256=runtime_digest,
                    resolved_spec_sha256=resolved_digest,
                    optimizer=settings,
                    mlx_rng_key=capture_mlx_rng_key(),
                    sampler=sampler.snapshot(),
                    objective=args.objective,
                    objective_options=options,
                    initial_target=initial,
                    reference_target=initial if args.objective == "dpo" else None,
                )
                store.save(
                    state,
                    {
                        key: value
                        for key, value in runtime.policy.parameters().items()
                        if key in runtime.policy.trainable_keys
                    },
                    optimizer.state,
                    artifact_id=uuid.UUID(int=5),
                )
                raise Paused()

    def batches(**kwargs: Any) -> Any:
        assert kwargs["comm_group"].size() == 1
        while True:
            batch = sampler.next_batch()
            yield (
                mx.array(batch.chosen_tokens + batch.rejected_tokens, dtype=mx.int32),
                mx.array(batch.chosen_masks + batch.rejected_masks, dtype=mx.bool_),
            )

    hook = make_preference_loss(
        args.objective, options, reference=runtime.reference.model if runtime.reference else None
    )
    try:
        train(
            runtime.policy.model,
            optimizer,
            train_dataset=sampler.dataset,
            val_dataset=None,
            args=TrainingArgs(
                batch_size=2,
                iters=(16 - offset) * 2,
                steps_per_report=2,
                steps_per_save=10000,
                max_seq_length=8,
                grad_accumulation_steps=2,
                adapter_file=str(directory / (args.mode + ".safetensors")),
            ),
            loss=hook,
            iterate_batches=batches,
            training_callback=Callback(),
        )
    except Paused:
        if args.mode != "pause":
            raise
    assert fingerprint() == reference_before
    if args.mode != "pause":
        path = directory / (args.mode + ".json")
        path.write_bytes(canonical_bytes(observations))
        path.chmod(0o600)
        path = directory / (args.mode + ".npz")
        np.savez(path, **arrays)
        path.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
