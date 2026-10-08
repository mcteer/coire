"""Deterministic complete-batch sampling with an explicit next-unconsumed cursor."""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections.abc import Sequence
from typing import Literal, Protocol, cast, overload

from pydantic import ValidationError

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training_node import MixtureSamplerState, SftBatch, SingleSourceSamplerState
from coire_core.training_sampling import mixture_epoch_order
from coire_node.training.datasets import IndexedTrainingSource, index_training_source

type SamplerState = SingleSourceSamplerState | MixtureSamplerState


class TrainingSampler(Protocol):
    @property
    def dataset(self) -> Sequence[TokenizedTrainingExample]: ...
    @property
    def batch_size(self) -> int: ...
    @property
    def dataset_sha256(self) -> str: ...
    def next_batch(self) -> SftBatch: ...
    def snapshot(self) -> SamplerState: ...
    def restore(self, state: SamplerState) -> None: ...


class SingleSourceSampler:
    def __init__(
        self,
        dataset: Sequence[TokenizedTrainingExample],
        *,
        dataset_sha256: str,
        batch_size: int,
        seed: int,
        max_sequence_length: int,
    ) -> None:
        if (
            not 1 <= len(dataset) <= 1_000_000
            or not 1 <= batch_size <= 64
            or len(dataset) % batch_size
            or not 2 <= max_sequence_length <= 8192
        ):
            raise TrainingValidationError("Training requires complete bounded source batches")
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise TrainingValidationError("Training sampler seed is invalid")
        if len(dataset_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in dataset_sha256
        ):
            raise TrainingValidationError("Training sampler requires an immutable source digest")
        self.dataset = dataset
        self.dataset_sha256 = dataset_sha256
        self.batch_size = batch_size
        self.max_sequence_length = max_sequence_length
        self.epoch = 0
        self.cursor = 0
        self._random = random.Random(seed)
        self._permutation = list(range(len(dataset)))
        self._random.shuffle(self._permutation)

    def next_batch(self) -> SftBatch:
        if self.cursor == len(self.dataset):
            self.epoch += 1
            self.cursor = 0
            self._permutation = list(range(len(self.dataset)))
            self._random.shuffle(self._permutation)
        indices = self._permutation[self.cursor : self.cursor + self.batch_size]
        examples = [self.dataset[index] for index in indices]
        if any(len(example.tokens) > self.max_sequence_length for example in examples):
            raise TrainingValidationError("Training sample exceeds the declared sequence bound")
        width = max(len(example.tokens) for example in examples)
        width = min(self.max_sequence_length, ((width + 31) // 32) * 32)
        batch = SftBatch(
            tokens=[example.tokens + [0] * (width - len(example.tokens)) for example in examples],
            target_masks=[
                example.target_mask + [False] * (width - len(example.target_mask))
                for example in examples
            ],
            source_rows=[example.source_row for example in examples],
        )
        # No speculative prefetch: advance only after the entire batch is validated.
        self.cursor += self.batch_size
        return batch

    def snapshot(self) -> SingleSourceSamplerState:
        version, state, gaussian = self._random.getstate()
        return SingleSourceSamplerState(
            dataset_sha256=self.dataset_sha256,
            row_count=len(self.dataset),
            batch_size=self.batch_size,
            max_sequence_length=self.max_sequence_length,
            epoch=self.epoch,
            cursor=self.cursor,
            permutation=list(self._permutation),
            rng_version=version,
            rng_state=list(state),
            gaussian_cache=gaussian,
        )

    def restore(self, state: SamplerState) -> None:
        if not isinstance(state, SingleSourceSamplerState):
            raise TrainingConflict("Checkpoint sampler algorithm differs from single-source inputs")
        try:
            state = SingleSourceSamplerState.model_validate(state.model_dump(mode="json"))
        except ValidationError:
            raise TrainingValidationError("Training sampler checkpoint is malformed") from None
        if (
            state.dataset_sha256 != self.dataset_sha256
            or state.row_count != len(self.dataset)
            or state.batch_size != self.batch_size
            or state.max_sequence_length != self.max_sequence_length
        ):
            raise TrainingConflict("Sampler checkpoint does not match immutable training inputs")
        self._random.setstate((state.rng_version, tuple(state.rng_state), state.gaussian_cache))
        self._permutation = list(state.permutation)
        self.epoch = state.epoch
        self.cursor = state.cursor


class IndexedExamples(Sequence[TokenizedTrainingExample]):
    """A cache view for upstream's dataset argument; never copy a merged token corpus."""

    def __init__(self, sources: Sequence[IndexedTrainingSource]) -> None:
        self.sources = tuple(sources)

    def __len__(self) -> int:
        return sum(len(source.examples) for source in self.sources)

    @overload
    def __getitem__(self, index: int) -> TokenizedTrainingExample: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[TokenizedTrainingExample]: ...
    def __getitem__(
        self, index: int | slice
    ) -> TokenizedTrainingExample | Sequence[TokenizedTrainingExample]:
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        for source in self.sources:
            if index < len(source.examples):
                return source.examples[index]
            index -= len(source.examples)
        raise IndexError(index)


class MixtureSampler:
    """Deterministic global stream with strided, disjoint rank assignment.

    batch_size is global and divisible by world_size. Short epoch tails continue into
    the next epoch, never dropped or silently padded with duplicate training rows.
    Each rank constructs the same global batch and advances the same global cursor.
    """

    def __init__(
        self,
        sources: Sequence[IndexedTrainingSource],
        *,
        mixture_sha256: str,
        batch_size: int,
        seed: int,
        max_sequence_length: int,
        strategy: Literal["weighted", "sequential"] = "weighted",
        replacement: bool = False,
        rank: int = 0,
        world_size: int = 1,
    ) -> None:
        if (
            not 1 <= len(sources) <= 16
            or len({s.dataset_id for s in sources}) != len(sources)
            or type(seed) is not int
            or not 0 <= seed < 2**32
            or type(batch_size) is not int
            or not 1 <= batch_size <= 64
            or type(world_size) is not int
            or world_size not in (1, 2)
            or type(rank) is not int
            or not 0 <= rank < world_size
            or batch_size % world_size
            or type(max_sequence_length) is not int
            or not 2 <= max_sequence_length <= 8192
            or strategy not in ("weighted", "sequential")
            or type(replacement) is not bool
            or len(mixture_sha256) != 64
            or any(c not in "0123456789abcdef" for c in mixture_sha256)
        ):
            raise TrainingValidationError("Mixture sampler configuration is invalid")
        self.sources = tuple(
            index_training_source(
                dataset_id=s.dataset_id,
                source_sha256=s.source_sha256,
                split_sha256=s.split_sha256,
                rows=s.rows,
                cache={e.source_row: e for e in s.examples},
                quota=s.quota,
            )
            for s in sources
        )
        self.epoch_samples = sum(s.quota for s in self.sources)
        if not 1 <= self.epoch_samples <= 16_000_000 or any(
            not replacement and s.quota > len(s.rows) for s in self.sources
        ):
            raise TrainingValidationError("Mixture quotas exceed source pools")
        if any(len(e.tokens) > max_sequence_length for s in self.sources for e in s.examples):
            raise TrainingValidationError("Training sample exceeds the declared sequence bound")
        self.batch_size, self.seed = batch_size, seed
        self.max_sequence_length, self.strategy = max_sequence_length, strategy
        self.replacement, self.rank = replacement, rank
        self.world_size = cast(Literal[1, 2], world_size)
        payload = [
            mixture_sha256,
            seed,
            strategy,
            replacement,
            batch_size,
            world_size,
            max_sequence_length,
            "sha256-counter-v1",
        ]
        digest = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode())
        # Hash cache entries incrementally rather than materializing their combined JSON.
        for source in self.sources:
            digest.update(
                json.dumps(
                    [
                        str(source.dataset_id),
                        source.source_sha256,
                        source.split_sha256,
                        source.rows,
                        source.quota,
                    ],
                    separators=(",", ":"),
                ).encode()
            )
            for example in source.examples:
                digest.update(
                    json.dumps(
                        [example.content_sha256, example.tokens, example.target_mask],
                        separators=(",", ":"),
                    ).encode()
                )
        self.identity_sha256 = digest.hexdigest()
        self.dataset_sha256 = self.identity_sha256
        self.dataset: Sequence[TokenizedTrainingExample] = IndexedExamples(self.sources)
        self.epoch = self.cursor = 0
        self._order = self._epoch_order(0)
        self.last_batch_references: tuple[tuple[uuid.UUID, int], ...] = ()

    def _epoch_order(self, epoch: int) -> list[tuple[int, int]]:
        return mixture_epoch_order(
            self.identity_sha256,
            epoch,
            [(len(source.rows), source.quota) for source in self.sources],
            strategy=self.strategy,
            replacement=self.replacement,
        )

    def next_batch(self) -> SftBatch:
        epoch, cursor, order = self.epoch, self.cursor, self._order
        references: list[tuple[int, int]] = []
        while len(references) < self.batch_size:
            if cursor == self.epoch_samples:
                epoch, cursor = epoch + 1, 0
                order = self._epoch_order(epoch)
            count = min(self.batch_size - len(references), self.epoch_samples - cursor)
            references.extend(order[cursor : cursor + count])
            cursor += count
        global_examples = [self.sources[source].examples[row] for source, row in references]
        examples = global_examples[self.rank :: self.world_size]
        width = min(
            self.max_sequence_length,
            ((max(len(e.tokens) for e in global_examples) + 31) // 32) * 32,
        )
        batch = SftBatch(
            tokens=[e.tokens + [0] * (width - len(e.tokens)) for e in examples],
            target_masks=[e.target_mask + [False] * (width - len(e.tokens)) for e in examples],
            source_rows=[e.source_row for e in examples],
        )
        self.epoch, self.cursor, self._order = epoch, cursor, order
        self.last_batch_references = tuple(
            (self.sources[source].dataset_id, self.sources[source].rows[row])
            for source, row in references[self.rank :: self.world_size]
        )
        return batch

    def snapshot(self) -> MixtureSamplerState:
        return MixtureSamplerState(
            identity_sha256=self.identity_sha256,
            epoch=self.epoch,
            cursor=self.cursor,
            rank=self.rank,
            world_size=self.world_size,
        )

    def restore(self, state: SamplerState) -> None:
        if not isinstance(state, MixtureSamplerState):
            raise TrainingConflict("Checkpoint sampler algorithm differs from mixture inputs")
        try:
            state = MixtureSamplerState.model_validate(state.model_dump(mode="json"))
        except ValidationError:
            raise TrainingValidationError("Mixture sampler checkpoint is malformed") from None
        if (
            not 0 <= state.cursor <= self.epoch_samples
            or (state.epoch * self.epoch_samples + state.cursor) % self.batch_size
        ):
            raise TrainingValidationError("Mixture sampler checkpoint is malformed")
        if (
            state.identity_sha256 != self.identity_sha256
            or state.rank != self.rank
            or state.world_size != self.world_size
        ):
            raise TrainingConflict(
                "Mixture sampler checkpoint does not match immutable inputs/rank"
            )
        order = self._epoch_order(state.epoch)
        self.epoch, self.cursor, self._order = state.epoch, state.cursor, order
        self.last_batch_references = ()
