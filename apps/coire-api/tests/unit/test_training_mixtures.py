"""Model-free compilation, grouped split and cross-source leakage checks."""

import uuid

import pytest

from coire_api.training.mixtures import compile_mixture, largest_remainder_quotas
from coire_core.errors import TrainingValidationError
from coire_core.models.datasets import (
    DatasetFormat,
    DatasetMixture,
    DatasetSource,
    SplitManifest,
    TokenizedTrainingExample,
)
from coire_core.training_data import normalize_row, split_digest, split_rows
from coire_node.training.datasets import index_training_source
from coire_node.training.sampler import MixtureSampler


def manifest(
    index: int, *, train: tuple[int, ...] = (1, 2, 3), hashes: tuple[int, ...] = (1, 2, 3, 4)
) -> SplitManifest:
    return SplitManifest(
        dataset_id=uuid.UUID(int=index),
        source_sha256=f"{index:064x}",
        seed=0,
        train_rows=list(train),
        validation_rows=[r for r in range(1, 5) if r not in train],
        row_content_sha256=[f"{h:064x}" for h in hashes],
    )


def mixture(
    *, replacement: bool = False, epoch_samples: int = 5, counts: tuple[int, int] = (3, 3)
) -> DatasetMixture:
    return DatasetMixture(
        datasets=[
            DatasetSource(
                dataset_id=uuid.UUID(int=1), sample_count=counts[0], mixture_proportion=0.5
            ),
            DatasetSource(
                dataset_id=uuid.UUID(int=2), sample_count=counts[1], mixture_proportion=0.5
            ),
        ],
        epoch_samples=epoch_samples,
        replacement=replacement,
        seed=0,
    )


def test_largest_remainder_normalizes_with_stable_ties_and_exact_total() -> None:
    assert largest_remainder_quotas([0.5, 0.5], 5) == (3, 2)
    assert largest_remainder_quotas([0.1, 0.2, 0.7], 7) == (1, 1, 5)
    assert largest_remainder_quotas([0.3333333] * 3, 2) == (1, 1, 0)
    for total in range(1, 300):
        quotas = largest_remainder_quotas([0.1, 0.2, 0.7], total)
        assert sum(quotas) == total
        assert all(
            abs(quota - weight * total) < 1
            for quota, weight in zip(quotas, [0.1, 0.2, 0.7], strict=True)
        )


@pytest.mark.parametrize("weights", [[float("nan")], [float("inf")], [0.0, 1.0], [0.4, 0.4]])
def test_invalid_proportions_are_refused(weights: list[float]) -> None:
    with pytest.raises(TrainingValidationError):
        largest_remainder_quotas(weights, 5)


def test_compilation_is_repeatable_and_pins_source_and_split_identity() -> None:
    manifests = {uuid.UUID(int=i): manifest(i) for i in (1, 2)}
    result = compile_mixture(mixture(), manifests)
    assert result == compile_mixture(mixture(), dict(reversed(list(manifests.items()))))
    assert [s.quota for s in result.sources] == [3, 2]
    assert result.sources[0].rows == (1, 2, 3)
    changed = dict(manifests)
    changed[uuid.UUID(int=1)] = manifest(1).model_copy(update={"source_sha256": "f" * 64})
    assert compile_mixture(mixture(), changed).identity_sha256 != result.identity_sha256
    limited = compile_mixture(mixture(counts=(2, 2), epoch_samples=4), manifests)
    assert limited.sources[0].rows == (1, 2)


def test_per_source_pool_limits_apply_even_when_aggregate_capacity_fits() -> None:
    manifests = {uuid.UUID(int=i): manifest(i) for i in (1, 2)}
    with pytest.raises(TrainingValidationError, match="quota"):
        compile_mixture(mixture(counts=(2, 3)), manifests)
    with pytest.raises(TrainingValidationError, match="sample_count"):
        compile_mixture(mixture(counts=(4, 4)), manifests)
    assert [
        s.quota
        for s in compile_mixture(mixture(replacement=True, epoch_samples=9), manifests).sources
    ] == [5, 4]


def test_conflicting_duplicate_membership_in_unselected_rows_is_refused() -> None:
    first = manifest(1)
    second = manifest(2, train=(1, 2, 3), hashes=(5, 6, 7, 3))
    # Content 3 is outside the selected first pool, yet still conflicts with held-out data.
    with pytest.raises(TrainingValidationError, match="duplicate split"):
        compile_mixture(
            mixture(counts=(1, 1), epoch_samples=2),
            {first.dataset_id: first, second.dataset_id: second},
        )
    with pytest.raises(TrainingValidationError, match="duplicate split"):
        compile_mixture(
            mixture(),
            {first.dataset_id: first, uuid.UUID(int=2): manifest(2)},
            validation_manifests=[manifest(3, hashes=(5, 6, 7, 1))],
        )


def test_missing_or_mismatched_manifest_is_refused() -> None:
    with pytest.raises(TrainingValidationError):
        compile_mixture(mixture(), {})
    with pytest.raises(TrainingValidationError):
        compile_mixture(mixture(), {uuid.UUID(int=1): manifest(2)})


def test_seed_zero_duplicate_group_splits_and_tiny_nonempty_partitions() -> None:
    for groups in range(2, 25):
        hashes = [f"{i:064x}" for i in range(groups) for _ in range(i % 3 + 1)]
        split = split_rows(uuid.UUID(int=1), "a" * 64, hashes, seed=0, validation_fraction=0.05)
        repeated = split_rows(uuid.UUID(int=1), "a" * 64, hashes, seed=0, validation_fraction=0.05)
        assert split == repeated
        assert split_digest(split) == split_digest(repeated)
        assert split.train_rows and split.validation_rows
        assert not (
            {hashes[r - 1] for r in split.train_rows}
            & {hashes[r - 1] for r in split.validation_rows}
        )
    with pytest.raises(TrainingValidationError):
        split_rows(uuid.UUID(int=1), "a" * 64, ["b" * 64] * 4, seed=0, validation_fraction=0.05)


def test_provenance_metadata_and_record_identity_do_not_change_content_hash() -> None:
    first = normalize_row(
        {"prompt": "question", "completion": "answer", "metadata": {"note": "private"}},
        format=DatasetFormat.PROMPT_COMPLETION,
        dataset_id=uuid.UUID(int=1),
        source_row=1,
    )
    second = normalize_row(
        {"prompt": "question", "completion": "answer"},
        format=DatasetFormat.PROMPT_COMPLETION,
        dataset_id=uuid.UUID(int=2),
        source_row=3,
    )
    assert first.content_sha256() == second.content_sha256()


def test_compiled_indices_feed_node_sampler_without_validation_rows() -> None:
    manifests = {uuid.UUID(int=i): manifest(i) for i in (1, 2)}
    compiled = compile_mixture(mixture(), manifests)
    indices = []
    for source in compiled.sources:
        cache = {
            row: TokenizedTrainingExample(
                source_row=row,
                content_sha256=manifests[source.dataset_id].row_content_sha256[row - 1],
                tokens=[1, source.dataset_id.int * 100 + row],
                target_mask=[False, True],
                target_start=1,
            )
            for row in range(1, 5)
        }
        indices.append(
            index_training_source(
                dataset_id=source.dataset_id,
                source_sha256=source.source_sha256,
                split_sha256=source.split_sha256,
                rows=source.rows,
                cache=cache,
                quota=source.quota,
            )
        )
    stream = MixtureSampler(
        indices,
        mixture_sha256=compiled.identity_sha256,
        seed=compiled.seed,
        strategy=compiled.strategy,
        replacement=compiled.replacement,
        batch_size=2,
        max_sequence_length=8,
    )
    sampled = [row[1] for _ in range(5) for row in stream.next_batch().tokens]
    assert 104 not in sampled and 204 not in sampled
    assert sum(row // 100 == 1 for row in sampled) == 6
    assert sum(row // 100 == 2 for row in sampled) == 4
