"""Pinned MLX key restoration and random-stream continuation, never on core."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from coire_node.training.checkpoints import capture_mlx_rng_key, restore_mlx_rng_key

pytestmark = pytest.mark.engine


def test_current_key_restores_subsequent_draws_and_original_seed_does_not(
    training_model: Path,
) -> None:
    import mlx.core as mx

    mx.random.seed(42)
    first = mx.random.uniform(shape=(16,))
    mx.eval(first)
    saved = capture_mlx_rng_key()
    expected = mx.random.uniform(shape=(16,))
    mx.eval(expected)
    restore_mlx_rng_key(saved)
    restored = mx.random.uniform(shape=(16,))
    mx.eval(restored)
    assert bool(mx.array_equal(restored, expected))
    mx.random.seed(42)
    wrong = mx.random.uniform(shape=(16,))
    mx.eval(wrong)
    assert not bool(mx.array_equal(wrong, expected))


@pytest.mark.parametrize("trial", [0, 1, 2])
@pytest.mark.parametrize("mixture", [False, True], ids=["single", "mixture"])
@pytest.mark.parametrize(
    "evaluation_pause", [False, True], ids=["ordinary-pause", "evaluation-pause"]
)
def test_bare_trainer_completed_update_full_state_resume(
    training_model: Path,
    tmp_path: Path,
    trial: int,
    mixture: bool,
    evaluation_pause: bool,
    training_kind: Literal["lora", "qlora", "dora"],
) -> None:
    """The unchanged trainer must unwind and continue a 32-update trajectory."""
    import uuid

    import mlx.core as mx
    import mlx.optimizers as optim
    import numpy as np
    from mlx.utils import tree_flatten
    from mlx_lm.tuner.callbacks import TrainingCallback
    from mlx_lm.tuner.trainer import TrainingArgs, train

    from coire_core.errors import TrainingValidationError
    from coire_core.models.datasets import TokenizedTrainingExample
    from coire_core.models.training import (
        LearningRateSchedule,
        TrainingOptimizer,
        TrainingParameterization,
    )
    from coire_core.models.training_node import CheckpointWorkerState
    from coire_node.training.checkpoints import CheckpointStore, apply_restored_checkpoint
    from coire_node.training.datasets import index_training_source
    from coire_node.training.loss import masked_sft_loss
    from coire_node.training.objectives import load_sft_runtime, validate_sft_input
    from coire_node.training.sampler import MixtureSampler, SingleSourceSampler, TrainingSampler

    flatten = cast(Callable[[Any], list[tuple[str, Any]]], tree_flatten)

    params = TrainingParameterization(
        kind=training_kind,
        rank=2,
        dropout=0.15,
        target_modules=["self_attn.q_proj", "self_attn.v_proj"],
    )
    source = validate_sft_input(training_model, params)
    settings = TrainingOptimizer(
        updates=36,
        accumulation_steps=2,
        max_sequence_length=8,
        learning_rate=1e-3,
        schedule=LearningRateSchedule(kind="warmup_linear", warmup_updates=2),
    )
    examples = [
        TokenizedTrainingExample(
            source_row=i + 1,
            content_sha256="a" * 64,
            tokens=[1, 2 + i, 8, 9],
            target_mask=[False, True, True, True],
            target_start=1,
        )
        for i in range(4)
    ]

    def make_sampler() -> TrainingSampler:
        if mixture:
            sources = [
                index_training_source(
                    dataset_id=uuid.UUID(int=index + 1),
                    source_sha256=f"{index + 1:064x}",
                    split_sha256=f"{index + 10:064x}",
                    rows=(1, 2, 3, 4),
                    cache={example.source_row: example for example in examples},
                    quota=quota,
                )
                for index, quota in enumerate((3, 2))
            ]
            return MixtureSampler(
                sources,
                mixture_sha256="b" * 64,
                batch_size=1,
                seed=trial,
                max_sequence_length=8,
            )
        return SingleSourceSampler(
            examples, dataset_sha256="b" * 64, batch_size=1, seed=trial, max_sequence_length=8
        )

    def make_optimizer() -> Any:
        def schedule(step: Any) -> Any:
            return settings.learning_rate * mx.where(
                step < 2, (step + 1) / 2, mx.maximum(0, (36 - step) / 34)
            )

        return optim.AdamW(learning_rate=schedule)

    class Paused(Exception):
        pass

    def run(
        runtime: Any,
        optimizer: Any,
        sampler: TrainingSampler,
        *,
        offset: int,
        pause_at: int | None,
        label: str,
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []

        def batches(**kwargs: Any) -> Any:
            assert kwargs["comm_group"].size() == 1
            while True:
                batch = sampler.next_batch()
                yield (
                    mx.array(batch.tokens, dtype=mx.int32),
                    mx.array(batch.target_masks, dtype=mx.bool_),
                )

        class Callback(TrainingCallback):
            def on_train_loss_report(self, info: dict[str, Any]) -> None:
                update = offset + info["iteration"] // 2
                assert optimizer.step.item() == update
                observations.append(
                    {
                        "update": update,
                        "loss": info["train_loss"],
                        "weights": {
                            key: np.array(value)
                            for key, value in flatten(runtime.model.trainable_parameters())
                        },
                        "optimizer": {
                            key: np.array(value) for key, value in flatten(optimizer.state)
                        },
                        "rng": capture_mlx_rng_key(),
                        "sampler": sampler.snapshot(),
                    }
                )
                if pause_at is not None and update == pause_at:
                    state = CheckpointWorkerState(
                        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        fence=1,
                        completed_update=update,
                        rank=0,
                        world_size=1,
                        runtime_sha256="c" * 64,
                        resolved_spec_sha256="d" * 64,
                        optimizer=settings,
                        mlx_rng_key=capture_mlx_rng_key(),
                        sampler=sampler.snapshot(),
                    )
                    manifest = store.save(
                        state,
                        dict(flatten(runtime.model.trainable_parameters())),
                        optimizer.state,
                    )
                    checkpoints.append(manifest.artifact_id)
                    if evaluation_pause:
                        from datetime import UTC, datetime, timedelta

                        from coire_core.models.training_node import (
                            CheckpointCommitAcknowledgementV2,
                            EvaluationCheckpointPause,
                            parse_checkpoint_acknowledgement,
                        )
                        from coire_node.training.worker import checkpoint_decision_action

                        decision = CheckpointCommitAcknowledgementV2(
                            command_id=uuid.uuid4(),
                            job_id=state.job_id,
                            attempt_id=state.attempt_id,
                            fence=state.fence,
                            request_sha256="e" * 64,
                            node="coire-edge-a",
                            rank=0,
                            world_size=1,
                            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                            checkpoint_id=manifest.artifact_id,
                            manifest_sha256=manifest.canonical_sha256(),
                            update=update,
                            committed_update=update,
                            job_version=2,
                            evaluation_pause=EvaluationCheckpointPause(
                                trigger_id=uuid.uuid4(), pause_command_id=uuid.uuid4()
                            ),
                        )
                        replay = parse_checkpoint_acknowledgement(decision.model_dump(mode="json"))
                        assert (
                            checkpoint_decision_action(replay, "continue", spec_version=2)
                            == "pause"
                        )
                    raise Paused()

        args = TrainingArgs(
            iters=(36 - offset) * 2,
            batch_size=1,
            grad_accumulation_steps=2,
            steps_per_report=2,
            steps_per_save=1000,
            max_seq_length=8,
            adapter_file=str(tmp_path / f"{label}-scratch.safetensors"),
        )
        if pause_at is not None:
            with pytest.raises(Paused):
                train(
                    runtime.model,
                    optimizer,
                    examples,
                    args=args,
                    loss=masked_sft_loss,
                    iterate_batches=batches,
                    training_callback=Callback(),
                )
        else:
            train(
                runtime.model,
                optimizer,
                examples,
                args=args,
                loss=masked_sft_loss,
                iterate_batches=batches,
                training_callback=Callback(),
            )
        return observations

    store = CheckpointStore(tmp_path / "checkpoints", disk_floor_bytes=0)
    checkpoints: list[Any] = []
    baseline = run(
        load_sft_runtime(source, seed=42 + trial),
        make_optimizer(),
        make_sampler(),
        offset=0,
        pause_at=None,
        label="baseline",
    )
    interrupted = run(
        load_sft_runtime(source, seed=42 + trial),
        make_optimizer(),
        make_sampler(),
        offset=0,
        pause_at=4,
        label="interrupted",
    )
    restored = store.restore(
        checkpoints[0], expected_runtime_sha256="c" * 64, expected_resolved_spec_sha256="d" * 64
    )
    runtime = load_sft_runtime(source, seed=999)
    optimizer = make_optimizer()
    sampler = make_sampler()
    apply_restored_checkpoint(restored, runtime, optimizer, sampler, expected_optimizer=settings)
    assert sampler.snapshot() == interrupted[-1]["sampler"]
    assert capture_mlx_rng_key() == interrupted[-1]["rng"]
    for key, value in flatten(optimizer.state):
        np.testing.assert_array_equal(np.array(value), interrupted[-1]["optimizer"][key])
    getter = cast(Callable[[], dict[str, Any]], runtime.model.trainable_parameters)
    for key, value in flatten(getter()):
        np.testing.assert_array_equal(np.array(value), interrupted[-1]["weights"][key])
    continued = run(
        runtime,
        optimizer,
        sampler,
        offset=4,
        pause_at=12 if evaluation_pause else None,
        label="resumed",
    )
    if evaluation_pause:
        assert len(checkpoints) == 2 and continued[-1]["update"] == 12
        # A second declared evaluation boundary restores into another fresh
        # trainer rather than retaining live optimizer/sampler/RNG objects.
        second = store.restore(
            checkpoints[1],
            expected_runtime_sha256="c" * 64,
            expected_resolved_spec_sha256="d" * 64,
        )
        runtime = load_sft_runtime(source, seed=777)
        optimizer, sampler = make_optimizer(), make_sampler()
        apply_restored_checkpoint(second, runtime, optimizer, sampler, expected_optimizer=settings)
        assert sampler.snapshot() == continued[-1]["sampler"]
        assert capture_mlx_rng_key() == continued[-1]["rng"]
        continued += run(
            runtime, optimizer, sampler, offset=12, pause_at=None, label="second-resume"
        )
    assert len(continued) == 32
    for expected, actual in zip(baseline[4:], continued, strict=True):
        assert expected["update"] == actual["update"]
        assert expected["rng"] == actual["rng"]
        assert expected["sampler"] == actual["sampler"]
        np.testing.assert_allclose(actual["loss"], expected["loss"], rtol=1e-4, atol=1e-5)
        for group in ("weights", "optimizer"):
            assert expected[group].keys() == actual[group].keys()
            for key in expected[group]:
                np.testing.assert_allclose(
                    actual[group][key], expected[group][key], rtol=1e-5, atol=1e-6
                )
    # Valid artifact headers still must agree with this exact destination runtime.
    adapter = dict(restored.adapter_tensors)
    key = next(iter(adapter))
    for invalid in (
        {name: value for name, value in adapter.items() if name != key},
        {**adapter, key: mx.zeros((1,), dtype=adapter[key].dtype)},
        {**adapter, key: adapter[key].astype(mx.float16)},
    ):
        restored.adapter_tensors = invalid
        with pytest.raises(TrainingValidationError, match="exact runtime"):
            apply_restored_checkpoint(
                restored, runtime, make_optimizer(), make_sampler(), expected_optimizer=settings
            )
    restored.adapter_tensors = adapter
    # A reset/missing moment is refused, rather than lazily initialized by MLX.
    restored.optimizer_state = {
        "step": mx.array(4, dtype=mx.uint64),
        "learning_rate": mx.array(1e-3),
    }
    with pytest.raises(TrainingValidationError, match="keys"):
        apply_restored_checkpoint(
            restored, runtime, make_optimizer(), make_sampler(), expected_optimizer=settings
        )
