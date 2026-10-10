"""Deterministic complete-pair sampling over bounded immutable rendered sources."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from pydantic import ValidationError

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import DatasetAnalysis, DatasetAnalysisBinding, DatasetFormat
from coire_core.models.preference import (
    PreferenceBatch,
    PreferenceRow,
    PreferenceSamplerState,
    PreferenceSplitManifest,
    TokenizedPreferenceExample,
    canonical_bytes,
)
from coire_core.models.training import ResolvedTrainingSpecV3
from coire_core.preference_data import compile_preference_mixture, preference_split_digest
from coire_core.training_sampling import mixture_epoch_order

if TYPE_CHECKING:
    from coire_core.models.training_node import TrainingPrepareRequest
    from coire_node.training.journal import TrainingJournal
    from coire_node.training.rendering import ChatTokenizer


@dataclass(frozen=True)
class PreferenceFrozenInputs:
    binding: DatasetAnalysisBinding
    split: PreferenceSplitManifest
    analysis: DatasetAnalysis
    source: Path


@dataclass(frozen=True)
class IndexedPreferenceSource:
    dataset_id: uuid.UUID
    split_sha256: str
    rows: tuple[int, ...]
    quota: int
    examples: Sequence[TokenizedPreferenceExample]


class PairedExamples(Sequence[TokenizedPreferenceExample]):
    def __init__(self, sources: Sequence[IndexedPreferenceSource]) -> None:
        self.sources = tuple(sources)

    def __len__(self) -> int:
        return sum(len(source.examples) for source in self.sources)

    @overload
    def __getitem__(self, index: int) -> TokenizedPreferenceExample: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[TokenizedPreferenceExample]: ...
    def __getitem__(
        self, index: int | slice
    ) -> TokenizedPreferenceExample | Sequence[TokenizedPreferenceExample]:
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


class PreferenceSampler:
    def __init__(
        self,
        sources: Sequence[IndexedPreferenceSource],
        *,
        mixture_sha256: str,
        batch_size: int,
        seed: int,
        max_sequence_length: int,
        strategy: Literal["weighted", "sequential"] = "weighted",
        replacement: bool = False,
    ) -> None:
        if (
            not 1 <= len(sources) <= 16
            or len({s.dataset_id for s in sources}) != len(sources)
            or type(seed) is not int
            or not 0 <= seed < 2**32
            or type(batch_size) is not int
            or not 1 <= batch_size <= 64
            or type(max_sequence_length) is not int
            or not 2 <= max_sequence_length <= 8192
            or type(replacement) is not bool
            or strategy not in {"weighted", "sequential"}
            or len(mixture_sha256) != 64
            or any(c not in "0123456789abcdef" for c in mixture_sha256)
        ):
            raise TrainingValidationError("Preference sampler configuration is invalid")
        for source in sources:
            if (
                not 1 <= len(source.rows) <= 1_000_000
                or len(source.rows) != len(source.examples)
                or len(set(source.rows)) != len(source.rows)
                or any(type(row) is not int or row < 1 for row in source.rows)
                or type(source.quota) is not int
                or not 0 <= source.quota <= 16_000_000
                or (not replacement and source.quota > len(source.rows))
                or len(source.split_sha256) != 64
                or any(c not in "0123456789abcdef" for c in source.split_sha256)
            ):
                raise TrainingValidationError("Preference source references are invalid")
        self.sources = tuple(sources)
        self.batch_size, self.seed, self.max_sequence_length = batch_size, seed, max_sequence_length
        self.strategy, self.replacement = strategy, replacement
        self.epoch_samples = sum(source.quota for source in sources)
        if not 1 <= self.epoch_samples <= 16_000_000:
            raise TrainingValidationError("Preference epoch is empty or exceeds bounded sampling")
        digest = hashlib.sha256(
            canonical_bytes(
                [
                    "coire-pair-sampler-v1",
                    mixture_sha256,
                    batch_size,
                    seed,
                    max_sequence_length,
                    strategy,
                    replacement,
                ]
            )
        )
        for source in sources:
            digest.update(
                canonical_bytes(
                    [str(source.dataset_id), source.split_sha256, source.rows, source.quota]
                )
            )
            for example in source.examples:
                digest.update(canonical_bytes(example.model_dump(mode="json")))
        self.dataset_sha256 = digest.hexdigest()
        self.dataset: Sequence[TokenizedPreferenceExample] = PairedExamples(self.sources)
        self.epoch = self.cursor = 0
        self._order = self._epoch_order(0)
        self.last_batch_references: tuple[tuple[uuid.UUID, int], ...] = ()

    def _epoch_order(self, epoch: int) -> list[tuple[int, int]]:
        return mixture_epoch_order(
            self.dataset_sha256,
            epoch,
            [(len(source.rows), source.quota) for source in self.sources],
            strategy=self.strategy,
            replacement=self.replacement,
        )

    def next_batch(self) -> PreferenceBatch:
        epoch, cursor, order = self.epoch, self.cursor, self._order
        references: list[tuple[int, int]] = []
        while len(references) < self.batch_size:
            if cursor == self.epoch_samples:
                epoch, cursor = epoch + 1, 0
                order = self._epoch_order(epoch)
            count = min(self.batch_size - len(references), self.epoch_samples - cursor)
            references.extend(order[cursor : cursor + count])
            cursor += count
        examples = [self.sources[source].examples[row] for source, row in references]
        longest = max(
            max(len(example.chosen_tokens), len(example.rejected_tokens)) for example in examples
        )
        if longest > self.max_sequence_length:
            raise TrainingValidationError("Preference pair exceeds declared sequence length")
        width = min(self.max_sequence_length, ((longest + 31) // 32) * 32)
        batch = PreferenceBatch(
            chosen_tokens=[
                e.chosen_tokens + [0] * (width - len(e.chosen_tokens)) for e in examples
            ],
            rejected_tokens=[
                e.rejected_tokens + [0] * (width - len(e.rejected_tokens)) for e in examples
            ],
            chosen_masks=[e.chosen_mask + [False] * (width - len(e.chosen_mask)) for e in examples],
            rejected_masks=[
                e.rejected_mask + [False] * (width - len(e.rejected_mask)) for e in examples
            ],
        )
        # Advance only after validating the entire complete pair batch.
        self.epoch, self.cursor, self._order = epoch, cursor, order
        self.last_batch_references = tuple(
            (self.sources[s].dataset_id, self.sources[s].rows[r]) for s, r in references
        )
        return batch

    def snapshot(self) -> PreferenceSamplerState:
        return PreferenceSamplerState(
            dataset_sha256=self.dataset_sha256,
            seed=self.seed,
            epoch=self.epoch,
            cursor=self.cursor,
            batch_size=self.batch_size,
        )

    def restore(self, state: PreferenceSamplerState) -> None:
        try:
            state = PreferenceSamplerState.model_validate(state.model_dump(mode="json"))
        except ValidationError:
            raise TrainingValidationError("Preference sampler checkpoint is malformed") from None
        if (
            state.dataset_sha256 != self.dataset_sha256
            or state.seed != self.seed
            or state.batch_size != self.batch_size
        ):
            raise TrainingConflict("Preference checkpoint differs from exact immutable inputs")
        if (
            not 0 <= state.epoch < 2**64
            or not 0 <= state.cursor <= self.epoch_samples
            or (state.epoch * self.epoch_samples + state.cursor) % self.batch_size
        ):
            raise TrainingValidationError("Preference checkpoint is not a consumed batch boundary")
        order = self._epoch_order(state.epoch)
        self.epoch, self.cursor, self._order = state.epoch, state.cursor, order
        self.last_batch_references = ()


def validate_preference_frozen_inputs(
    prepared: TrainingPrepareRequest, inputs: PreferenceFrozenInputs
) -> None:
    from coire_node.training.worker import payload_sha256

    resolved = prepared.resolved
    if (
        not isinstance(resolved, ResolvedTrainingSpecV3)
        or prepared.world_size != 1
        or prepared.rank != 0
    ):
        raise TrainingValidationError("Paired source compilation requires single-node v3 intent")
    spec = resolved.spec
    binding, split, analysis = inputs.binding, inputs.split, inputs.analysis
    selected = next(
        (item for item in resolved.datasets if item.dataset_id == binding.dataset_id), None
    )
    source = next(
        (item for item in spec.data.train.datasets if item.dataset_id == binding.dataset_id), None
    )
    if selected is None or (
        source is None and binding.dataset_id not in spec.data.validation.dataset_ids
    ):
        raise TrainingConflict("Preference source is outside frozen intent")
    if source is not None and source.sample_count > len(split.train_rows):
        raise TrainingConflict("Preference source pool differs from frozen split")
    if (
        binding.format != DatasetFormat.PREFERENCE
        or binding.model_id != spec.model.model_id
        or binding.variant_id != spec.model.variant_id
        or binding.base_manifest_sha256 != resolved.base_manifest_sha256
        or binding.source_sha256 != selected.source_sha256
        or binding.split_sha256 != selected.split_sha256
        or binding.enable_thinking != resolved.enable_thinking
        or split.dataset_id != selected.dataset_id
        or split.source_sha256 != selected.source_sha256
        or preference_split_digest(split) != selected.split_sha256
    ):
        raise TrainingConflict("Preference model/source/split differs from exact resolved identity")
    paired = analysis.preference
    if (
        analysis.id != selected.analysis_id
        or analysis.dataset_id != selected.dataset_id
        or analysis.model_id != spec.model.model_id
        or analysis.variant_id != spec.model.variant_id
        or analysis.state != "succeeded"
        or analysis.invalid_count
        or analysis.row_count != len(split.row_content_sha256)
        or analysis.tokenizer_sha256 != resolved.tokenizer_sha256
        or analysis.template_sha256 != resolved.template_sha256
        or analysis.runtime_sha256 != resolved.runtime_sha256
        or payload_sha256(analysis) != selected.analysis_sha256
        or paired is None
        or paired.source_sha256 != selected.source_sha256
        or paired.split_sha256 != selected.split_sha256
        or paired.dataset_id != selected.dataset_id
        or paired.row_count != len(split.row_content_sha256)
        or paired.prompt_group_count != len(set(split.prompt_group_sha256))
        or paired.tokenizer_sha256 != resolved.tokenizer_sha256
        or paired.template_sha256 != resolved.template_sha256
        or paired.runtime_sha256 != resolved.runtime_sha256
    ):
        raise TrainingConflict("Preference compilation requires exact successful paired analysis")
    if (
        binding.template_override is not None
        and hashlib.sha256(binding.template_override.encode()).hexdigest()
        != resolved.template_sha256
    ):
        raise TrainingConflict("Preference effective template differs from frozen digest")


def load_preference_frozen_inputs(
    prepared: TrainingPrepareRequest, directory: Path, journal: TrainingJournal
) -> list[PreferenceFrozenInputs]:
    from coire_node.training.worker import read_private, verify_source

    if not isinstance(prepared.resolved, ResolvedTrainingSpecV3):
        raise TrainingValidationError("Preference inputs require explicit v3 intent")
    record = journal.get(prepared.attempt_id)
    sources = record.get("input_sources")
    legacy = record.get("input_files")
    if legacy is not None and len(prepared.resolved.datasets) != 1:
        raise TrainingConflict("Legacy staging cannot cover multiple preference sources")
    result = []
    for selected in prepared.resolved.datasets:
        expected = legacy if legacy is not None else (sources or {}).get(str(selected.dataset_id))
        root = directory if legacy is not None else directory / str(selected.dataset_id)
        if not isinstance(expected, dict) or set(expected) != {
            "binding.json",
            "split.json",
            "analysis.json",
            "source.jsonl",
        }:
            raise TrainingConflict("Node-owned preference inputs have not been staged")
        metadata = {}
        for name in ("binding.json", "split.json", "analysis.json"):
            encoded = read_private(root / name, 96 * 1024**2)
            if hashlib.sha256(encoded).hexdigest() != expected[name]:
                raise TrainingConflict("Node-owned preference metadata changed after staging")
            metadata[name] = encoded
        source = root / "source.jsonl"
        verify_source(source, expected["source.jsonl"])
        inputs = PreferenceFrozenInputs(
            DatasetAnalysisBinding.model_validate_json(metadata["binding.json"]),
            PreferenceSplitManifest.model_validate_json(metadata["split.json"]),
            DatasetAnalysis.model_validate_json(metadata["analysis.json"]),
            source,
        )
        validate_preference_frozen_inputs(prepared, inputs)
        result.append(inputs)
    return result


def compile_preference_samples(
    prepared: TrainingPrepareRequest,
    inputs: Sequence[PreferenceFrozenInputs],
    tokenizer: ChatTokenizer,
    *,
    buffer_observer: Callable[[int], None] | None = None,
) -> tuple[PreferenceSampler, PreferenceSampler]:
    from coire_node.training.rendering import render_preference_example
    from coire_node.training.worker import verify_source

    resolved = prepared.resolved
    if not isinstance(resolved, ResolvedTrainingSpecV3):
        raise TrainingValidationError("Preference compilation requires explicit v3")
    spec = resolved.spec
    sources = {item.binding.dataset_id: item for item in inputs}
    ids = {item.dataset_id for item in spec.data.train.datasets} | set(
        spec.data.validation.dataset_ids
    )
    if (
        len(sources) != len(inputs)
        or set(sources) != ids
        or set(sources) != {item.dataset_id for item in resolved.datasets}
    ):
        raise TrainingConflict("Preference sources must cover the exact resolved revision set")
    for item in inputs:
        validate_preference_frozen_inputs(prepared, item)
    if len({item.binding.model_slug for item in inputs}) != 1:
        raise TrainingConflict("Preference sources disagree on acquired base identity")
    mixture = compile_preference_mixture(
        spec.data.train,
        {key: item.split for key, item in sources.items()},
        validation_manifests=[sources[key].split for key in spec.data.validation.dataset_ids],
    )
    selected = {item.dataset_id: set(item.rows) for item in mixture.sources}
    validation_ids = set(spec.data.validation.dataset_ids)
    estimated = (
        mixture.epoch_samples
        + sum(len(sources[key].split.validation_rows) for key in validation_ids)
    ) * 256

    def check_buffer() -> None:
        if buffer_observer is not None:
            buffer_observer(estimated)
        if estimated > resolved.resource_envelope.buffer_bytes:
            raise TrainingValidationError("Preference cache exceeds accounted buffer envelope")

    check_buffer()
    caches: dict[uuid.UUID, dict[int, TokenizedPreferenceExample]] = {}
    for key, item in sources.items():
        verify_source(item.source, item.binding.source_sha256)
        needed = selected.get(key, set()) | (
            set(item.split.validation_rows) if key in validation_ids else set()
        )
        cache: dict[int, TokenizedPreferenceExample] = {}
        count = 0
        with item.source.open("rb") as stream:
            while encoded := stream.readline(256 * 1024 + 2):
                count += 1
                if len(encoded.rstrip(b"\n")) > 256 * 1024 or count > len(
                    item.split.row_content_sha256
                ):
                    raise TrainingValidationError(
                        "Preference source exceeds frozen row/byte bounds"
                    )
                try:
                    example = PreferenceRow.model_validate(json.loads(encoded))
                except (ValueError, UnicodeError, RecursionError):
                    raise TrainingValidationError(
                        "Preference source row violates frozen schema"
                    ) from None
                if (
                    example.content_sha256() != item.split.row_content_sha256[count - 1]
                    or example.prompt_sha256() != item.split.prompt_group_sha256[count - 1]
                ):
                    raise TrainingConflict("Preference semantic content differs from frozen split")
                if count in needed:
                    rendered = render_preference_example(
                        example,
                        tokenizer,
                        source_row=count,
                        max_sequence_length=spec.optim.max_sequence_length,
                        enable_thinking=resolved.enable_thinking,
                    )
                    estimated += (
                        len(rendered.chosen_tokens) + len(rendered.rejected_tokens)
                    ) * 192 + 8192
                    check_buffer()
                    cache[count] = rendered
        if count != len(item.split.row_content_sha256):
            raise TrainingConflict("Preference source count differs from frozen split")
        caches[key] = cache
    training = [
        IndexedPreferenceSource(
            item.dataset_id,
            item.split_sha256,
            item.rows,
            item.quota,
            tuple(caches[item.dataset_id][row] for row in item.rows),
        )
        for item in mixture.sources
    ]
    validation = [
        IndexedPreferenceSource(
            key,
            sources[key].binding.split_sha256,
            tuple(sorted(sources[key].split.validation_rows)),
            len(sources[key].split.validation_rows),
            tuple(caches[key][row] for row in sorted(sources[key].split.validation_rows)),
        )
        for key in spec.data.validation.dataset_ids
    ]
    validation_digest = hashlib.sha256(
        canonical_bytes(
            [
                spec.data.validation.model_dump(mode="json"),
                [item.split_sha256 for item in validation],
            ]
        )
    ).hexdigest()
    return (
        PreferenceSampler(
            training,
            mixture_sha256=mixture.identity_sha256,
            batch_size=spec.optim.batch_size,
            seed=mixture.seed,
            max_sequence_length=spec.optim.max_sequence_length,
            strategy=mixture.strategy,
            replacement=mixture.replacement,
        ),
        PreferenceSampler(
            validation,
            mixture_sha256=validation_digest,
            batch_size=1,
            seed=spec.data.validation.seed,
            max_sequence_length=spec.optim.max_sequence_length,
            strategy="sequential",
        ),
    )
