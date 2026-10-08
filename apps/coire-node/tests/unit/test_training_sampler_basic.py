"""Single-source batches preserve exact cursor/RNG state and exclude padding targets."""

import pytest

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import TokenizedTrainingExample
from coire_node.training.sampler import SingleSourceSampler


def examples() -> list[TokenizedTrainingExample]:
    return [
        TokenizedTrainingExample(
            source_row=index + 1,
            content_sha256="a" * 63 + str(index),
            tokens=[1, 2, 10 + index],
            target_mask=[False, False, True],
            target_start=2,
        )
        for index in range(4)
    ]


def sampler(seed: int = 0) -> SingleSourceSampler:
    return SingleSourceSampler(
        examples(), dataset_sha256="b" * 64, batch_size=2, seed=seed, max_sequence_length=8
    )


def test_seed_zero_and_checkpoint_cursor_reproduce_across_epoch_boundaries() -> None:
    original = sampler()
    original.next_batch()
    state = original.snapshot()
    expected = [original.next_batch().model_dump() for _ in range(12)]
    restored = sampler()
    restored.restore(state)
    assert [restored.next_batch().model_dump() for _ in range(12)] == expected
    assert restored.snapshot().epoch > 1


def test_padded_positions_cannot_enter_next_token_loss() -> None:
    batch = sampler().next_batch()
    assert len(batch.tokens) == 2
    assert all(len(row) == 8 for row in batch.tokens)
    assert all(sum(mask[1:]) == 1 for mask in batch.target_masks)
    assert all(not any(mask[3:]) for mask in batch.target_masks)


def test_incomplete_batch_and_overlength_are_rejected_without_truncation() -> None:
    with pytest.raises(TrainingValidationError):
        SingleSourceSampler(
            examples(), dataset_sha256="b" * 64, batch_size=3, seed=0, max_sequence_length=8
        )
    data = examples()
    data[0] = data[0].model_copy(
        update={"tokens": [1] * 9, "target_mask": [False] * 2 + [True] * 7}
    )
    source = SingleSourceSampler(
        data, dataset_sha256="b" * 64, batch_size=2, seed=0, max_sequence_length=8
    )
    with pytest.raises(TrainingValidationError):
        for _ in range(2):
            source.next_batch()


def test_restore_refuses_different_inputs_and_corrupt_permutation() -> None:
    state = sampler().snapshot()
    with pytest.raises(TrainingConflict):
        sampler().restore(state.model_copy(update={"dataset_sha256": "c" * 64}))
    with pytest.raises(TrainingValidationError):
        sampler().restore(state.model_copy(update={"permutation": [0, 0, 1, 2]}))


def test_evaluation_single_source_replay_matches_cursor_and_rejects_changed_seed() -> None:
    from coire_core.training_sampling import consumed_single_rows

    original = sampler()
    expected: set[int] = set()
    original.next_batch()
    expected.update(original.snapshot().permutation[:2])
    state = original.snapshot()
    assert consumed_single_rows(state, seed=0, rows=[1, 2, 3, 4]) == {
        index + 1 for index in expected
    }
    with pytest.raises(ValueError, match="boundary"):
        consumed_single_rows(state, seed=1, rows=[1, 2, 3, 4])
    for _ in range(6):
        expected.update(index - 1 for index in original.next_batch().source_rows)
    assert consumed_single_rows(original.snapshot(), seed=0, rows=[1, 2, 3, 4]) == {
        index + 1 for index in expected
    }
