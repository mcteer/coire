"""Paired sampling preserves exact next-consumed state without a native runtime."""

import random
import uuid
from dataclasses import replace

import pytest

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.preference import TokenizedPreferenceExample
from coire_node.training.preference_data import IndexedPreferenceSource, PreferenceSampler


def source(start: int = 0, count: int = 3) -> IndexedPreferenceSource:
    examples = [
        TokenizedPreferenceExample(
            source_row=index + 1,
            content_sha256=f"{index + start:064x}",
            prompt_sha256=f"{index + start:064x}",
            chosen_tokens=[1, index + start + 2, 9],
            rejected_tokens=[1, index + start + 3],
            chosen_mask=[False, True, True],
            rejected_mask=[False, True],
            prompt_length=1,
        )
        for index in range(count)
    ]
    return IndexedPreferenceSource(
        dataset_id=uuid.UUID(int=start + 1),
        split_sha256="a" * 64,
        rows=tuple(range(1, count + 1)),
        quota=count,
        examples=examples,
    )


def sampler(sources: list[IndexedPreferenceSource], seed: int = 42) -> PreferenceSampler:
    return PreferenceSampler(
        sources, mixture_sha256="b" * 64, batch_size=2, seed=seed, max_sequence_length=32
    )


def test_pair_masks_and_complete_batches_cross_epoch_tail_without_global_rng_changes() -> None:
    before = random.getstate()
    runtime = sampler([source()])
    first = runtime.next_batch()
    assert len(first.chosen_tokens) == len(first.rejected_tokens) == 2
    assert sum(map(sum, first.chosen_masks)) == 4 and sum(map(sum, first.rejected_masks)) == 2
    assert all(not mask[0] and not any(mask[3:]) for mask in first.chosen_masks)
    runtime.next_batch()
    assert runtime.snapshot().epoch == 1 and runtime.snapshot().cursor == 1
    assert random.getstate() == before


def test_full_state_restore_matches_next_batches_and_source_references() -> None:
    sources = [source(), source(10, 4)]
    uninterrupted = sampler(sources)
    uninterrupted.next_batch()
    state = uninterrupted.snapshot()
    restarted = sampler(sources)
    restarted.restore(state)
    for _ in range(10):
        assert uninterrupted.next_batch() == restarted.next_batch()
        assert uninterrupted.last_batch_references == restarted.last_batch_references
        assert uninterrupted.snapshot() == restarted.snapshot()


def test_changed_input_seed_or_non_consumed_boundary_is_refused() -> None:
    initial = sampler([source()])
    initial.next_batch()
    checkpoint = initial.snapshot()
    with pytest.raises(TrainingConflict):
        sampler([source()], seed=43).restore(checkpoint)
    changed = source()
    changed = replace(
        changed,
        examples=[
            changed.examples[0].model_copy(update={"chosen_tokens": [1, 7, 9]}),
            *changed.examples[1:],
        ],
    )
    with pytest.raises(TrainingConflict):
        sampler([changed]).restore(checkpoint)
    with pytest.raises(TrainingValidationError):
        initial.restore(checkpoint.model_copy(update={"cursor": 1}))


def test_oversize_pair_refusal_does_not_advance_cursor() -> None:
    item = source(count=1)
    item = replace(
        item,
        examples=[
            item.examples[0].model_copy(
                update={"chosen_tokens": [1] * 40, "chosen_mask": [False] + [True] * 39}
            )
        ],
    )
    runtime = sampler([item])
    before = runtime.snapshot()
    with pytest.raises(TrainingValidationError):
        runtime.next_batch()
    assert runtime.snapshot() == before
