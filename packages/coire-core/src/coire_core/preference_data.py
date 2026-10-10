"""Model-free preference identity and whole-prompt-group partitioning."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence

from coire_core.errors import TrainingValidationError
from coire_core.models.datasets import DatasetMixture
from coire_core.models.preference import PreferenceRow, PreferenceSplitManifest, canonical_bytes
from coire_core.training_data import (
    CompiledMixture,
    CompiledMixtureSource,
    largest_remainder_quotas,
)


def split_preference_rows(
    dataset_id: uuid.UUID,
    source_sha256: str,
    rows: Sequence[PreferenceRow],
    *,
    seed: int,
    validation_fraction: float,
) -> PreferenceSplitManifest:
    return split_preference_hashes(
        dataset_id,
        source_sha256,
        [row.content_sha256() for row in rows],
        [row.prompt_sha256() for row in rows],
        seed=seed,
        validation_fraction=validation_fraction,
    )


def split_preference_hashes(
    dataset_id: uuid.UUID,
    source_sha256: str,
    content_hashes: list[str],
    prompt_hashes: list[str],
    *,
    seed: int,
    validation_fraction: float,
) -> PreferenceSplitManifest:
    if len(content_hashes) != len(prompt_hashes):
        raise TrainingValidationError("Preference row and prompt identities must align")
    if not math.isfinite(validation_fraction) or not 0 < validation_fraction < 1:
        raise TrainingValidationError("Preference validation fraction is invalid")
    groups: dict[str, list[int]] = defaultdict(list)
    for index, digest in enumerate(prompt_hashes, start=1):
        groups[digest].append(index)
    if len(groups) < 2:
        raise TrainingValidationError(
            "Preference datasets need at least two distinct prompt groups"
        )
    ordered = sorted(
        groups,
        key=lambda digest: hashlib.sha256(
            f"coire-preference-split-v1:{seed}:{digest}".encode()
        ).digest(),
    )
    target = max(1, round(len(content_hashes) * validation_fraction))
    selected: set[str] = set()
    count = 0
    for digest in ordered[:-1]:
        selected.add(digest)
        count += len(groups[digest])
        if count >= target:
            break
    return PreferenceSplitManifest(
        dataset_id=dataset_id,
        source_sha256=source_sha256,
        seed=seed,
        validation_fraction=validation_fraction,
        train_rows=sorted(
            index for digest in groups if digest not in selected for index in groups[digest]
        ),
        validation_rows=sorted(index for digest in selected for index in groups[digest]),
        row_content_sha256=content_hashes,
        prompt_group_sha256=prompt_hashes,
    )


def preference_split_digest(manifest: PreferenceSplitManifest) -> str:
    return hashlib.sha256(canonical_bytes(manifest.model_dump(mode="json"))).hexdigest()


def detect_preference_leakage(manifests: Sequence[PreferenceSplitManifest]) -> None:
    train: set[str] = set()
    validation: set[str] = set()
    for manifest in manifests:
        # Revalidate so callers cannot bypass invariants via model_copy/update.
        manifest = PreferenceSplitManifest.model_validate(manifest.model_dump(mode="json"))
        train.update(manifest.prompt_group_sha256[index - 1] for index in manifest.train_rows)
        validation.update(
            manifest.prompt_group_sha256[index - 1] for index in manifest.validation_rows
        )
    if train & validation:
        raise TrainingValidationError(
            "Preference sources place the same prompt in training and validation"
        )


def compile_preference_mixture(
    mixture: DatasetMixture,
    manifests: Mapping[uuid.UUID, PreferenceSplitManifest],
    *,
    validation_manifests: Sequence[PreferenceSplitManifest] = (),
) -> CompiledMixture:
    """Reuse model-free source quotas while freezing preference-specific identities."""
    mixture = DatasetMixture.model_validate(mixture.model_dump(mode="json"))
    selected = []
    for source in mixture.datasets:
        manifest = manifests.get(source.dataset_id)
        if (
            not isinstance(manifest, PreferenceSplitManifest)
            or manifest.dataset_id != source.dataset_id
        ):
            raise TrainingValidationError("Preference mixture requires matching preference splits")
        selected.append(PreferenceSplitManifest.model_validate(manifest.model_dump(mode="json")))
    detect_preference_leakage([*selected, *validation_manifests])
    quotas = largest_remainder_quotas(
        [source.mixture_proportion for source in mixture.datasets], mixture.epoch_samples
    )
    compiled = []
    for source, manifest, quota in zip(mixture.datasets, selected, quotas, strict=True):
        if source.sample_count > len(manifest.train_rows) or (
            not mixture.replacement and quota > source.sample_count
        ):
            raise TrainingValidationError("Preference source quota exceeds its immutable pool")
        compiled.append(
            CompiledMixtureSource(
                dataset_id=source.dataset_id,
                source_sha256=manifest.source_sha256,
                split_sha256=preference_split_digest(manifest),
                rows=tuple(sorted(manifest.train_rows)[: source.sample_count]),
                quota=quota,
            )
        )
    payload = {
        "algorithm": "coire-preference-mixture-v1",
        "intent": mixture.model_dump(mode="json"),
        "splits": [source.split_sha256 for source in compiled],
        "validation_splits": sorted(preference_split_digest(item) for item in validation_manifests),
        "quotas": quotas,
    }
    return CompiledMixture(
        tuple(compiled),
        hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        mixture.seed,
        mixture.mixture_strategy,
        mixture.replacement,
        mixture.epoch_samples,
    )
