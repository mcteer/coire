"""CPU-only mixture order, quota, rank and exact recovery tests."""

import uuid
from collections import Counter
from typing import Literal

import pytest

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training_node import MixtureSamplerState
from coire_node.training.datasets import IndexedTrainingSource, index_training_source
from coire_node.training.sampler import MixtureSampler


def sources(quotas: tuple[int, int] = (3, 2)) -> list[IndexedTrainingSource]:
    result = []
    for source, quota in enumerate(quotas):
        cache = {
            row: TokenizedTrainingExample(
                source_row=row,
                content_sha256=f"{source * 100 + row:064x}",
                tokens=[1, source * 100 + row],
                target_mask=[False, True],
                target_start=1,
            )
            for row in range(1, 5)
        }
        result.append(
            index_training_source(
                dataset_id=uuid.UUID(int=source + 1),
                source_sha256=f"{source + 10:064x}",
                split_sha256=f"{source + 20:064x}",
                rows=(1, 2, 3, 4),
                cache=cache,
                quota=quota,
            )
        )
    return result


def sampler(
    *,
    rank: int = 0,
    world_size: int = 1,
    batch_size: int = 2,
    strategy: Literal["weighted", "sequential"] = "weighted",
    replacement: bool = False,
    quotas: tuple[int, int] = (3, 2),
) -> MixtureSampler:
    return MixtureSampler(
        sources(quotas),
        mixture_sha256="a" * 64,
        batch_size=batch_size,
        seed=0,
        max_sequence_length=8,
        strategy=strategy,
        replacement=replacement,
        rank=rank,
        world_size=world_size,
    )


@pytest.mark.parametrize("strategy", ["weighted", "sequential"])
@pytest.mark.parametrize("replacement", [False, True])
def test_exact_recovery_across_partial_epoch_and_source_boundaries(
    strategy: Literal["weighted", "sequential"],
    replacement: bool,
) -> None:
    uninterrupted = sampler(strategy=strategy, replacement=replacement)
    uninterrupted.next_batch()
    uninterrupted.next_batch()
    saved = uninterrupted.snapshot()
    assert (saved.epoch, saved.cursor) == (0, 4)
    saved = MixtureSamplerState.model_validate_json(saved.model_dump_json())
    expected = [uninterrupted.next_batch().model_dump() for _ in range(19)]
    recovered = sampler(strategy=strategy, replacement=replacement)
    recovered.restore(saved)
    assert [recovered.next_batch().model_dump() for _ in range(19)] == expected
    assert recovered.snapshot() == uninterrupted.snapshot()


@pytest.mark.parametrize("strategy", ["weighted", "sequential"])
def test_each_epoch_has_exact_quotas_without_dropped_tail(
    strategy: Literal["weighted", "sequential"],
) -> None:
    stream = sampler(batch_size=1, strategy=strategy)
    for _ in range(6):
        rows = [stream.next_batch().tokens[0][1] for _ in range(5)]
        assert Counter(row // 100 for row in rows) == {0: 3, 1: 2}
        assert len(set(rows)) == 5
        if strategy == "sequential":
            assert [row // 100 for row in rows] == [0, 0, 0, 1, 1]
    # A global batch of two crosses the five-row epoch without losing its last row.
    batched = sampler(batch_size=2, strategy=strategy)
    # Batch size is intentionally pinned in the identity; compare quota accounting,
    # rather than pretending differently configured jobs have the same permutation.
    rows = [row[1] for _ in range(5) for row in batched.next_batch().tokens]
    assert Counter(row // 100 for row in rows) == {0: 6, 1: 4}
    assert (batched.snapshot().epoch, batched.snapshot().cursor) == (1, 5)


def test_rank_partition_reconstructs_global_batches_and_recovers_both_ranks() -> None:
    ranks = [sampler(rank=rank, world_size=2, batch_size=4) for rank in range(2)]
    for _ in range(7):
        batches = [rank.next_batch() for rank in ranks]
        assert batches[0].tokens != batches[1].tokens
        assert ranks[0].snapshot().epoch == ranks[1].snapshot().epoch
        assert ranks[0].snapshot().cursor == ranks[1].snapshot().cursor
        for stream, batch in zip(ranks, batches, strict=True):
            assert [
                100 * (dataset_id.int - 1) + row for dataset_id, row in stream.last_batch_references
            ] == [tokens[1] for tokens in batch.tokens]
    for rank in range(2):
        state = ranks[rank].snapshot()
        recovered = sampler(rank=rank, world_size=2, batch_size=4)
        recovered.restore(state)
        assert [recovered.next_batch().model_dump() for _ in range(8)] == [
            ranks[rank].next_batch().model_dump() for _ in range(8)
        ]


def test_replacement_can_draw_beyond_pool_and_zero_quota_source_is_not_sampled() -> None:
    stream = sampler(quotas=(9, 0), replacement=True, batch_size=1)
    rows = [stream.next_batch().tokens[0][1] for _ in range(9)]
    assert all(1 <= row <= 4 for row in rows)
    assert len(set(rows)) < len(rows)
    with pytest.raises(TrainingValidationError):
        sampler(quotas=(9, 0))


def test_restore_refuses_changed_identity_rank_version_or_cursor_without_mutation() -> None:
    stream = sampler()
    saved = stream.snapshot()
    for changed in [
        saved.model_copy(update={"cursor": -1}),
        saved.model_copy(update={"cursor": 1}),
        saved.model_copy(update={"cursor": 6}),
        saved.model_copy(update={"epoch": True}),
        saved.model_copy(update={"generator_version": "unknown"}),
        saved.model_copy(update={"rank": 1}),
    ]:
        with pytest.raises(TrainingValidationError):
            stream.restore(changed)
        assert stream.snapshot() == saved
    for changed in [
        saved.model_copy(update={"identity_sha256": "b" * 64}),
        saved.model_copy(update={"world_size": 2}),
    ]:
        with pytest.raises(TrainingConflict):
            stream.restore(changed)
        assert stream.snapshot() == saved
    with pytest.raises(TrainingConflict):
        sampler(strategy="sequential").restore(saved)
    with pytest.raises(TrainingConflict):
        sampler(quotas=(2, 3)).restore(saved)


def test_cache_indices_are_checked_and_caller_mutation_cannot_change_samples() -> None:
    source = sources()[0]
    with pytest.raises(TrainingValidationError):
        index_training_source(
            dataset_id=source.dataset_id,
            source_sha256=source.source_sha256,
            split_sha256=source.split_sha256,
            rows=(1, 1),
            cache={},
            quota=1,
        )
    with pytest.raises(TrainingValidationError):
        index_training_source(
            dataset_id=source.dataset_id,
            source_sha256=source.source_sha256,
            split_sha256=source.split_sha256,
            rows=(5,),
            cache={},
            quota=1,
        )
    inputs = sources()
    stream = MixtureSampler(
        inputs, mixture_sha256="a" * 64, batch_size=2, seed=0, max_sequence_length=8
    )
    expected = sampler().next_batch()
    for item in inputs:
        for example in item.examples:
            example.tokens[1] = 999
    assert stream.next_batch() == expected


@pytest.mark.parametrize("rank,world_size,batch_size", [(0, 2, 3), (2, 2, 4), (0, 3, 6)])
def test_invalid_rank_assignment_is_refused(rank: int, world_size: int, batch_size: int) -> None:
    with pytest.raises(TrainingValidationError):
        sampler(rank=rank, world_size=world_size, batch_size=batch_size)


def test_versioned_sample_order_golden_vector() -> None:
    stream = sampler()
    assert [row[1] for _ in range(5) for row in stream.next_batch().tokens] == [
        104,
        3,
        1,
        2,
        103,
        1,
        102,
        103,
        2,
        4,
    ]


def test_rank_assignment_golden_vector_includes_tail_crossing() -> None:
    ranks = [sampler(rank=rank, world_size=2, batch_size=4) for rank in range(2)]
    combined: list[int] = []
    for _ in range(3):
        batches = [stream.next_batch() for stream in ranks]
        combined.extend(
            batches[rank].tokens[position][1] for position in range(2) for rank in range(2)
        )
    assert combined == [4, 102, 3, 104, 1, 101, 102, 4, 2, 3, 104, 3]


def test_global_padding_shape_and_validation_iterator_independence() -> None:
    inputs = sources((1, 1))
    for example in inputs[1].examples:
        example.tokens = [1] * 39 + [example.tokens[1]]
        example.target_mask = [False] * 39 + [True]
        example.target_start = 39
    ranks = [
        MixtureSampler(
            inputs,
            mixture_sha256="a" * 64,
            batch_size=2,
            seed=0,
            max_sequence_length=64,
            rank=rank,
            world_size=2,
        )
        for rank in range(2)
    ]
    batches = [stream.next_batch() for stream in ranks]
    assert [len(batch.tokens[0]) for batch in batches] == [64, 64]
    assert all(sum(batch.target_masks[0]) == 1 for batch in batches)
    train = sampler()
    saved = train.snapshot()
    validation = sampler()
    for _ in range(13):
        validation.next_batch()
    assert train.snapshot() == saved
    assert train.next_batch() == sampler().next_batch()
