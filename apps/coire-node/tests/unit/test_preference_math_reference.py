"""CPU-only independent FP64 objective and analytic-gradient checks."""

import math
from collections.abc import Callable

import pytest
from preference_math_reference import dpo_reference, orpo_reference


@pytest.mark.parametrize(
    "chosen,rejected", [(-0.4, -2.0), (-2.0, -0.4), (-1000.0, -900.0), (-1e-5, -0.001)]
)
def test_scalar_oracles_agree_with_finite_difference(chosen: float, rejected: float) -> None:
    objectives: tuple[Callable[[float, float], tuple[float, tuple[float, float]]], ...] = (
        lambda c, r: dpo_reference(c, r, -1.0, -2.0, 0.1),
        lambda c, r: orpo_reference(c, r, 0.1),
    )
    step = min(1e-6, abs(chosen) * 1e-4, abs(rejected) * 1e-4)
    for objective in objectives:
        loss, gradient = objective(chosen, rejected)
        assert math.isfinite(loss)
        numeric_c = (
            objective(chosen + step, rejected)[0] - objective(chosen - step, rejected)[0]
        ) / (2 * step)
        numeric_r = (
            objective(chosen, rejected + step)[0] - objective(chosen, rejected - step)[0]
        ) / (2 * step)
        assert gradient == pytest.approx((numeric_c, numeric_r), rel=1e-4, abs=1e-5)


def test_zero_weight_does_not_evaluate_odds() -> None:
    assert orpo_reference(-0.2, float("nan"), 0) == (0.2, (-1.0, 0.0))


def test_dpo_equal_initial_policy_has_exact_log_two_loss() -> None:
    assert dpo_reference(-3, -7, -3, -7, 0.1)[0] == math.log(2)


def test_loss_factory_requires_matching_options_and_exact_reference() -> None:
    from coire_core.errors import TrainingValidationError
    from coire_core.models.preference import DpoOptions, OrpoOptions
    from coire_node.training.preference_loss import make_preference_loss

    with pytest.raises(TrainingValidationError):
        make_preference_loss("dpo", DpoOptions(beta=0.1))
    with pytest.raises(TrainingValidationError):
        make_preference_loss("dpo", OrpoOptions(weight=0.1), reference=lambda tokens: tokens)
    with pytest.raises(TrainingValidationError):
        make_preference_loss("orpo", OrpoOptions(weight=0.1), reference=lambda tokens: tokens)
    assert callable(make_preference_loss("orpo", OrpoOptions(weight=0.0)))
