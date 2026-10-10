"""Independent scalar FP64 oracle for the normative pair objectives.

This module never imports a model, tokenizer or native tensor runtime; engine tests
compare production FP32 values/gradients with these scalar equations.
"""

import math


def softplus(value: float) -> float:
    return max(value, 0.0) + math.log1p(math.exp(-abs(value)))


def dpo_reference(
    chosen: float, rejected: float, reference_chosen: float, reference_rejected: float, beta: float
) -> tuple[float, tuple[float, float]]:
    margin = beta * ((chosen - rejected) - (reference_chosen - reference_rejected))
    factor = -beta * math.exp(-softplus(margin))
    return softplus(-margin), (factor, -factor)


def orpo_reference(
    chosen: float, rejected: float, weight: float
) -> tuple[float, tuple[float, float]]:
    if weight == 0:
        return -chosen, (-1.0, 0.0)
    c, r = min(chosen, -1e-7), min(rejected, -1e-7)
    # Independent scalar math.expm1 path avoids cancellation near zero.
    odds_c = c - math.log(-math.expm1(c))
    odds_r = r - math.log(-math.expm1(r))
    margin = odds_c - odds_r
    penalty = softplus(-margin)
    factor = weight * math.exp(-softplus(margin))
    dc = -1.0 - (factor / -math.expm1(c) if chosen < -1e-7 else 0.0)
    dr = factor / -math.expm1(r) if rejected < -1e-7 else 0.0
    return -chosen + weight * penalty, (dc, dr)
