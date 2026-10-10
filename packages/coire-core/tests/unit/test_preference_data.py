"""Whole prompt groups stay isolated regardless of response orientation."""

import uuid

import pytest

from coire_core.errors import TrainingValidationError
from coire_core.models.preference import PreferenceRow
from coire_core.preference_data import detect_preference_leakage, split_preference_rows


def row(prompt: str, chosen: str = "yes", rejected: str = "no") -> PreferenceRow:
    return PreferenceRow.model_validate(
        {"prompt": [{"role": "user", "content": prompt}], "chosen": chosen, "rejected": rejected}
    )


def test_split_is_deterministic_and_groups_opposing_answers() -> None:
    rows = [row("a"), row("a", "no", "yes"), row("b"), row("c")]
    identity = uuid.uuid4()
    first = split_preference_rows(identity, "a" * 64, rows, seed=3, validation_fraction=0.25)
    assert first == split_preference_rows(
        identity, "a" * 64, rows, seed=3, validation_fraction=0.25
    )
    assert (1 in first.train_rows) == (2 in first.train_rows)
    assert first.prompt_group_sha256[0] == first.prompt_group_sha256[1]
    assert first.row_content_sha256[0] != first.row_content_sha256[1]


def test_single_prompt_cannot_produce_ready_split() -> None:
    with pytest.raises(TrainingValidationError):
        split_preference_rows(
            uuid.uuid4(),
            "a" * 64,
            [row("a"), row("a", "no", "yes")],
            seed=0,
            validation_fraction=0.5,
        )


def test_cross_source_prompt_leakage_refuses_different_answers() -> None:
    first = split_preference_rows(
        uuid.uuid4(), "a" * 64, [row("a"), row("b")], seed=0, validation_fraction=0.5
    )
    second = first.model_copy(deep=True)
    second.train_rows, second.validation_rows = first.validation_rows, first.train_rows
    with pytest.raises(TrainingValidationError):
        detect_preference_leakage([first, second])


def test_preference_mixture_uses_whole_prompt_groups_and_exact_identity() -> None:
    from coire_core.models.datasets import DatasetMixture
    from coire_core.preference_data import compile_preference_mixture

    identity = uuid.uuid4()
    manifest = split_preference_rows(
        identity, "a" * 64, [row("a"), row("b"), row("c")], seed=0, validation_fraction=0.25
    )
    mixture = DatasetMixture.model_validate(
        {
            "datasets": [
                {"dataset_id": str(identity), "sample_count": 2, "mixture_proportion": 1.0}
            ],
            "epoch_samples": 2,
        }
    )
    first = compile_preference_mixture(
        mixture, {identity: manifest}, validation_manifests=[manifest]
    )
    assert first.sources[0].rows == tuple(manifest.train_rows)
    assert first == compile_preference_mixture(
        mixture, {identity: manifest}, validation_manifests=[manifest]
    )
