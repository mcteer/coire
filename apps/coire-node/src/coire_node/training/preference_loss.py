"""Pure tensor objectives for the unchanged bare mlx-lm trainer loss hook.

The iterator supplies chosen rows followed by rejected rows. Upstream aggregation
counts pairs; response-token accounting belongs to the iterator/callback, never
Python side effects inside a compiled loss.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from coire_core.errors import TrainingValidationError
from coire_core.models.preference import DpoOptions, OrpoOptions

if TYPE_CHECKING:
    import mlx.core as mx


type ModelForward = Callable[[mx.array], mx.array]
type LossHook = Callable[[ModelForward, mx.array, mx.array], tuple[mx.array, mx.array]]


def response_logps(
    model: ModelForward, tokens: mx.array, masks: mx.array
) -> tuple[mx.array, mx.array]:
    import mlx.core as mx
    from mlx.nn.losses import cross_entropy

    logits = model(tokens[:, :-1]).astype(mx.float32)
    logps = -cross_entropy(logits, tokens[:, 1:], reduction="none")
    targets = masks[:, 1:]
    sums = mx.where(targets, logps, mx.array(0.0, dtype=mx.float32)).sum(axis=-1)
    return sums, targets.sum(axis=-1)


def softplus(value: mx.array) -> mx.array:
    import mlx.core as mx

    return mx.maximum(value, 0) + mx.log1p(mx.exp(-mx.abs(value)))


def log1mexp(value: mx.array) -> mx.array:
    import mlx.core as mx

    # Both branches are defined for negative finite arguments. The near-zero
    # branch avoids subtracting two nearly equal FP32 values.
    bounded = mx.minimum(value.astype(mx.float32), mx.array(-1e-7, dtype=mx.float32))
    return mx.where(
        bounded < -0.6931471805599453, mx.log1p(-mx.exp(bounded)), mx.log(-mx.expm1(bounded))
    )


def dpo_pair_losses(
    chosen: mx.array,
    rejected: mx.array,
    reference_chosen: mx.array,
    reference_rejected: mx.array,
    *,
    beta: float,
) -> mx.array:
    import mlx.core as mx

    reference_margin = mx.stop_gradient(
        reference_chosen.astype(mx.float32) - reference_rejected.astype(mx.float32)
    )
    margin = beta * (chosen.astype(mx.float32) - rejected.astype(mx.float32) - reference_margin)
    return softplus(-margin)


def orpo_pair_losses(chosen_mean: mx.array, rejected_mean: mx.array, *, weight: float) -> mx.array:
    import mlx.core as mx

    nll = -chosen_mean.astype(mx.float32)
    if weight == 0:
        return nll
    chosen = mx.minimum(chosen_mean.astype(mx.float32), mx.array(-1e-7, dtype=mx.float32))
    rejected = mx.minimum(rejected_mean.astype(mx.float32), mx.array(-1e-7, dtype=mx.float32))
    margin = (chosen - log1mexp(chosen)) - (rejected - log1mexp(rejected))
    return nll + weight * softplus(-margin)


def make_preference_loss(
    objective: Literal["dpo", "orpo"],
    options: DpoOptions | OrpoOptions,
    *,
    reference: ModelForward | None = None,
) -> LossHook:
    if objective == "dpo":
        if not isinstance(options, DpoOptions) or reference is None:
            raise TrainingValidationError("DPO requires its exact frozen initial-policy reference")
    elif not isinstance(options, OrpoOptions) or reference is not None:
        raise TrainingValidationError("ORPO options must not include a reference")

    def loss(model: ModelForward, tokens: mx.array, masks: mx.array) -> tuple[mx.array, mx.array]:
        import mlx.core as mx

        if (
            tokens.ndim != 2
            or tokens.shape[0] < 2
            or tokens.shape[0] % 2
            or tokens.shape != masks.shape
        ):
            raise TrainingValidationError("Preference loss requires aligned complete pairs")
        pairs = tokens.shape[0] // 2
        sums, counts = response_logps(model, tokens, masks)
        if isinstance(options, DpoOptions):
            assert reference is not None
            reference_sums, _ = response_logps(reference, tokens, masks)
            losses = dpo_pair_losses(
                sums[:pairs],
                sums[pairs:],
                reference_sums[:pairs],
                reference_sums[pairs:],
                beta=options.beta,
            )
        else:
            means = sums / counts.astype(mx.float32)
            losses = orpo_pair_losses(means[:pairs], means[pairs:], weight=options.weight)
        return losses.mean(), mx.array(pairs, dtype=mx.int32)

    return loss
