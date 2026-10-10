"""Post-update probes preserve native state, including when a forward fails."""

import uuid
from pathlib import Path
from typing import Literal

import pytest

from coire_core.models.adapters import InferenceTarget
from coire_core.models.preference import DpoOptions, OrpoOptions, TokenizedPreferenceExample
from coire_core.models.training import TrainingParameterization

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
@pytest.mark.parametrize("fail", [False, True])
def test_probe_preserves_rng_modes_sampler_and_parameters(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
    objective: Literal["dpo", "orpo"],
    fail: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mlx.core as mx

    from coire_node.training import preference_probe
    from coire_node.training.checkpoints import capture_mlx_rng_key, restore_mlx_rng_key
    from coire_node.training.preference_data import IndexedPreferenceSource, PreferenceSampler
    from coire_node.training.preference_runtime import (
        load_preference_runtime,
        validate_preference_input,
    )

    source = validate_preference_input(
        training_model,
        TrainingParameterization(
            kind=training_kind, rank=2, target_modules=["self_attn.q_proj", "self_attn.v_proj"]
        ),
    )
    target = InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        base_manifest_sha256=source.manifest.sha256(),
    )
    runtime = load_preference_runtime(source, seed=42, objective=objective, initial_target=target)
    examples = [
        TokenizedPreferenceExample(
            source_row=i + 1,
            content_sha256=f"{i:064x}",
            prompt_sha256=f"{i:064x}",
            chosen_tokens=[1, 2, 3, 4],
            rejected_tokens=[1, 2, 5],
            chosen_mask=[False, False, True, True],
            rejected_mask=[False, False, True],
            prompt_length=2,
        )
        for i in range(10)
    ]
    validation = PreferenceSampler(
        [IndexedPreferenceSource(uuid.uuid4(), "a" * 64, tuple(range(1, 11)), 10, examples)],
        mixture_sha256="b" * 64,
        batch_size=2,
        seed=42,
        max_sequence_length=32,
    )
    validation.next_batch()
    state, references = validation.snapshot(), validation.last_batch_references
    runtime.policy.model.train()
    parameters = {key: mx.array(value) for key, value in runtime.policy.parameters().items()}
    mx.eval(parameters)
    rng = capture_mlx_rng_key()
    expected_draw = mx.random.uniform(shape=(4,))
    mx.eval(expected_draw)
    restore_mlx_rng_key(rng)
    options = DpoOptions(beta=0.1) if objective == "dpo" else OrpoOptions(weight=0)
    if fail:

        def broken(*args: object, **kwargs: object) -> object:
            mx.eval(mx.random.uniform(shape=(4,)))
            raise RuntimeError("injected forward failure")

        monkeypatch.setattr(preference_probe, "response_logps", broken)
        with pytest.raises(RuntimeError, match="injected"):
            preference_probe.post_update_probe(runtime, validation, options)
    else:
        result = preference_probe.post_update_probe(runtime, validation, options)
        assert result.sample_count == 8
        if objective == "dpo":
            assert result.accuracy == 0 and result.margin == 0
            assert result.chosen_nll is None and result.odds_penalty is None
        else:
            assert result.chosen_nll is not None and result.chosen_nll > 0
            assert result.odds_penalty == 0
    assert capture_mlx_rng_key() == rng
    assert runtime.policy.model.training
    if runtime.reference is not None:
        assert not runtime.reference.model.training
    assert validation.snapshot() == state and validation.last_batch_references == references
    assert all(
        bool(mx.array_equal(value, runtime.policy.parameters()[key]))
        for key, value in parameters.items()
    )
    actual_draw = mx.random.uniform(shape=(4,))
    mx.eval(actual_draw)
    assert bool(mx.array_equal(expected_draw, actual_draw))
