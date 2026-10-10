"""FP32 preference losses/gradients against independent scalar FP64 equations."""

from pathlib import Path

import pytest
from preference_math_reference import dpo_reference, orpo_reference

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
@pytest.mark.parametrize(
    "chosen,rejected", [(-0.4, -2.0), (-2.0, -0.4), (-1000.0, -900.0), (-1e-5, -0.001)]
)
def test_fp32_loss_and_gradients_match_independent_reference(
    training_model: Path, objective: str, chosen: float, rejected: float
) -> None:
    import mlx.core as mx

    from coire_node.training.preference_loss import dpo_pair_losses, orpo_pair_losses

    values = mx.array([chosen, rejected], dtype=mx.float32)

    def loss(values: mx.array) -> mx.array:
        if objective == "dpo":
            return dpo_pair_losses(
                values[:1], values[1:], mx.array([-1.0]), mx.array([-2.0]), beta=0.1
            ).mean()
        return orpo_pair_losses(values[:1], values[1:], weight=0.1).mean()

    actual = loss(values)
    gradients = mx.grad(loss)(values)
    mx.eval(actual, gradients)
    expected, expected_gradients = (
        dpo_reference(chosen, rejected, -1.0, -2.0, 0.1)
        if objective == "dpo"
        else orpo_reference(chosen, rejected, 0.1)
    )
    assert actual.item() == pytest.approx(expected, rel=1e-5, abs=1e-5)
    assert gradients.tolist() == pytest.approx(expected_gradients, rel=1e-4, abs=1e-5)


def test_response_logps_shift_masks_exclude_prompt_and_padding(training_model: Path) -> None:
    import math

    import mlx.core as mx

    from coire_node.training.preference_loss import response_logps

    # Prompt/padding target losses overflow if evaluated as half precision, and
    # must not contribute. Response log-softmax is computed in FP32.
    logits = mx.array(
        [[[60000.0, -60000.0, 0.0], [0.0, 2.0, 0.0], [-60000.0, 60000.0, 0.0]]], dtype=mx.float16
    )
    tokens = mx.array([[0, 1, 1, 0]], dtype=mx.int32)
    masks = mx.array([[False, False, True, False]], dtype=mx.bool_)
    sums, counts = response_logps(lambda _: logits, tokens, masks)
    mx.eval(sums, counts)
    assert counts.tolist() == [1]
    assert sums.tolist() == pytest.approx([-math.log(1 + 2 * math.exp(-2))], rel=1e-5, abs=1e-5)
    assert sums.dtype == mx.float32


def test_reference_gradients_are_detached_and_orpo_zero_skips_odds(training_model: Path) -> None:
    import mlx.core as mx

    from coire_node.training.preference_loss import dpo_pair_losses, orpo_pair_losses

    reference = mx.array([-1.0, -2.0])
    gradients = mx.grad(
        lambda r: dpo_pair_losses(mx.array([-3.0]), mx.array([-4.0]), r[:1], r[1:], beta=0.1).mean()
    )(reference)
    zero = orpo_pair_losses(mx.array([-0.2]), mx.array([float("nan")]), weight=0.0)
    mx.eval(gradients, zero)
    assert gradients.tolist() == [0.0, 0.0]
    assert zero.item() == pytest.approx(0.2, abs=1e-7)


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_paired_hook_uses_pair_count_and_objective_specific_sequence_normalization(
    training_model: Path, objective: str
) -> None:
    import math

    import mlx.core as mx

    from coire_core.models.preference import DpoOptions, OrpoOptions
    from coire_node.training.preference_loss import make_preference_loss

    # Chosen has two response targets, rejected has one. Equal token logps
    # produce different sequence sums but equal per-response means.
    tokens = mx.array([[0, 1, 1], [0, 1, 0]], dtype=mx.int32)
    masks = mx.array([[False, True, True], [False, True, False]], dtype=mx.bool_)

    def policy(_: mx.array) -> mx.array:
        return mx.array([[[0.0, 2.0], [0.0, 2.0]], [[0.0, 2.0], [0.0, 2.0]]])

    def reference(_: mx.array) -> mx.array:
        return mx.zeros((2, 2, 2), dtype=mx.float32)

    token_logp = -math.log1p(math.exp(-2))
    hook = (
        make_preference_loss("dpo", DpoOptions(beta=0.1), reference=reference)
        if objective == "dpo"
        else make_preference_loss("orpo", OrpoOptions(weight=0.1))
    )
    loss, pairs = hook(policy, tokens, masks)
    expected = (
        dpo_reference(2 * token_logp, token_logp, -2 * math.log(2), -math.log(2), 0.1)[0]
        if objective == "dpo"
        else orpo_reference(token_logp, token_logp, 0.1)[0]
    )
    mx.eval(loss, pairs)
    assert pairs.item() == 1
    assert loss.item() == pytest.approx(expected, rel=1e-5, abs=1e-5)


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_equal_pair_microbatches_preserve_mean_loss_and_accumulated_gradient(
    training_model: Path, objective: str
) -> None:
    import mlx.core as mx

    from coire_node.training.preference_loss import dpo_pair_losses, orpo_pair_losses

    values = mx.array([[-0.4, -2.0], [-3.0, -0.5]], dtype=mx.float32)

    def vector_loss(values: mx.array) -> mx.array:
        if objective == "dpo":
            return dpo_pair_losses(
                values[:, 0],
                values[:, 1],
                mx.full((values.shape[0],), -1.0),
                mx.full((values.shape[0],), -2.0),
                beta=0.1,
            ).mean()
        return orpo_pair_losses(values[:, 0], values[:, 1], weight=0.1).mean()

    full = vector_loss(values)
    split = (vector_loss(values[:1]) + vector_loss(values[1:])) / 2
    full_grad = mx.grad(vector_loss)(values)
    split_grad = mx.grad(lambda v: (vector_loss(v[:1]) + vector_loss(v[1:])) / 2)(values)
    mx.eval(full, split, full_grad, split_grad)
    assert full.item() == pytest.approx(split.item(), rel=1e-5, abs=1e-5)
    assert full_grad.reshape(-1).tolist() == pytest.approx(
        split_grad.reshape(-1).tolist(), rel=1e-4, abs=1e-5
    )
