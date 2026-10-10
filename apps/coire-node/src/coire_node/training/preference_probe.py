"""Bounded post-update observations that leave training execution unchanged."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from coire_core.errors import TrainingValidationError
from coire_core.models.preference import DpoOptions, OrpoOptions, PreferenceProbe
from coire_node.training.checkpoints import capture_mlx_rng_key, restore_mlx_rng_key
from coire_node.training.preference_loss import log1mexp, response_logps, softplus

if TYPE_CHECKING:
    from coire_node.training.preference_data import PreferenceSampler
    from coire_node.training.preference_loss import ModelForward
    from coire_node.training.preference_runtime import PreferenceRuntime


def post_update_probe(
    runtime: PreferenceRuntime,
    validation: PreferenceSampler,
    options: DpoOptions | OrpoOptions,
) -> PreferenceProbe:
    """Use the first at most eight frozen held-out pairs, one forward at a time."""
    import mlx.core as mx

    count = min(8, len(validation.dataset))
    if not count:
        raise TrainingValidationError("Preference probe requires held-out pairs")
    if (runtime.objective == "dpo" and not isinstance(options, DpoOptions)) or (
        runtime.objective == "orpo" and not isinstance(options, OrpoOptions)
    ):
        raise TrainingValidationError("Preference probe options differ from objective")
    models = [runtime.policy.model]
    if runtime.reference is not None:
        models.append(runtime.reference.model)
    modes = [model.training for model in models]
    key = capture_mlx_rng_key()
    state = validation.snapshot()
    correct = 0
    margins: list[float] = []
    nlls: list[float] = []
    penalties: list[float] = []
    try:
        for model in models:
            model.eval()
        for index in range(count):
            example = validation.dataset[index]
            width = max(len(example.chosen_tokens), len(example.rejected_tokens))
            if width > validation.max_sequence_length:
                raise TrainingValidationError("Preference probe pair exceeds sequence bound")
            tokens = mx.array(
                [
                    row + [0] * (width - len(row))
                    for row in (example.chosen_tokens, example.rejected_tokens)
                ],
                dtype=mx.int32,
            )
            masks = mx.array(
                [
                    row + [False] * (width - len(row))
                    for row in (example.chosen_mask, example.rejected_mask)
                ],
                dtype=mx.bool_,
            )
            sums, lengths = response_logps(
                cast("ModelForward", runtime.policy.model), tokens, masks
            )
            if isinstance(options, DpoOptions):
                if runtime.reference is None:
                    raise TrainingValidationError("DPO probe requires frozen initial reference")
                reference, _ = response_logps(
                    cast("ModelForward", runtime.reference.model), tokens, masks
                )
                margin = options.beta * (sums[0] - sums[1] - reference[0] + reference[1])
            else:
                means = sums / lengths
                chosen = mx.minimum(means[0], mx.array(-1e-7, dtype=mx.float32))
                rejected = mx.minimum(means[1], mx.array(-1e-7, dtype=mx.float32))
                margin = chosen - log1mexp(chosen) - rejected + log1mexp(rejected)
                penalty = softplus(-margin) if options.weight else mx.array(0.0)
                mx.eval(means, penalty)
                nlls.append(float(cast(float, (-means[0]).item())))
                penalties.append(float(cast(float, penalty.item())))
            mx.eval(margin)
            value = float(cast(float, margin.item()))
            margins.append(value)
            correct += int(value > 0)
        return PreferenceProbe(
            sample_count=count,
            accuracy=correct / count,
            margin=sum(margins) / count,
            chosen_nll=sum(nlls) / count if nlls else None,
            odds_penalty=sum(penalties) / count if penalties else None,
        )
    finally:
        for model, mode in zip(models, modes, strict=True):
            model.train(mode)
        restore_mlx_rng_key(key)
        if validation.snapshot() != state:
            raise TrainingValidationError("Preference probe advanced held-out sampler")
