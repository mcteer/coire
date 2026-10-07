"""Pinned bare loader/adapter compatibility on an explicitly enabled non-core Mac."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

import pytest

from coire_core.models.training import TrainingParameterization
from coire_node.training.objectives import load_sft_runtime, validate_sft_input

pytestmark = pytest.mark.engine


def test_acquired_base_loads_offline_and_only_adapter_parameters_are_trainable(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
) -> None:
    # The fixture verifies local acquisition bytes first and refuses model work on core.
    import mlx.core as mx
    import mlx.optimizers as optim
    from mlx.nn.layers.base import Module
    from mlx.nn.losses import cross_entropy
    from mlx.nn.utils import value_and_grad

    parameters = TrainingParameterization(
        kind=training_kind, rank=2, target_modules=["self_attn.q_proj", "self_attn.v_proj"]
    )
    source = validate_sft_input(training_model, parameters)
    runtime = load_sft_runtime(source, seed=42)
    assert runtime.trainable_keys
    suffixes = (".lora_a", ".lora_b", ".m") if training_kind == "dora" else (".lora_a", ".lora_b")
    assert all(key.endswith(suffixes) for key in runtime.trainable_keys)
    assert any(key.endswith(".m") for key in runtime.trainable_keys) == (training_kind == "dora")
    before = dict(runtime.frozen_parameters)
    tokens = mx.array([[1, 2, 3]], dtype=mx.int32)

    def loss(model: Module) -> mx.array:
        logits = cast(Callable[[mx.array], mx.array], model)(tokens[:, :-1])
        return cross_entropy(logits, tokens[:, 1:], reduction="mean")

    optimizer = optim.AdamW(learning_rate=1e-3)
    value, gradients = value_and_grad(runtime.model, loss)(runtime.model)
    optimizer.update(runtime.model, gradients)
    mx.eval(runtime.parameters(), optimizer.state, value)
    after = runtime.current_frozen_parameters()
    assert before.keys() == after.keys()
    assert all(bool(mx.array_equal(before[key], after[key])) for key in before)


def test_masked_loss_excludes_the_first_padded_target(training_model: Path) -> None:
    import math

    import mlx.core as mx

    from coire_node.training.loss import masked_sft_loss

    logits = mx.array([[[0.0, 2.0, 0.0], [-99.0, 99.0, 0.0]]], dtype=mx.float32)
    tokens = mx.array([[1, 1, 0]], dtype=mx.int32)
    mask = mx.array([[False, True, False]], dtype=mx.bool_)
    loss, count = masked_sft_loss(lambda _tokens: logits, tokens, mask)
    mx.eval(loss, count)
    assert count.item() == 1
    value = loss.item()
    assert isinstance(value, float)
    assert abs(value - math.log(1 + 2 * math.exp(-2))) < 1e-6
