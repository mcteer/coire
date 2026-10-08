"""Immutable private training-input identity; no paths, credentials or tensor payloads."""

import uuid

from pydantic import AwareDatetime, Field, model_validator

from coire_core.models.datasets import DatasetFormat, DatasetMixture
from coire_core.models.training_types import Digest, Seed, StudioName, TrainingId, TrainingWire


class EvaluationTrainingSource(TrainingWire):
    dataset_id: uuid.UUID
    format: DatasetFormat
    source_sha256: Digest
    source_bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)
    split_sha256: Digest
    split_bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)


class EvaluationTrainingBinding(TrainingWire):
    job_id: TrainingId
    training_attempt_id: TrainingId
    training_fence: int = Field(strict=True, ge=1, le=2**31 - 1)
    checkpoint_id: uuid.UUID
    checkpoint_manifest_sha256: Digest
    resolved_spec_sha256: Digest
    runtime_sha256: Digest
    completed_update: int = Field(strict=True, ge=1, le=100_000)
    batch_size: int = Field(strict=True, ge=1, le=64)
    accumulation_steps: int = Field(strict=True, ge=1, le=64)
    seed: Seed
    mixture: DatasetMixture
    state_file_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    state_sha256: Digest
    state_bytes: int = Field(strict=True, ge=1, le=8 * 1024**2)
    sources: list[EvaluationTrainingSource] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def exact_sources(self) -> "EvaluationTrainingBinding":
        ids = [source.dataset_id for source in self.sources]
        if len(set(ids)) != len(ids) or ids != [
            source.dataset_id for source in self.mixture.datasets
        ]:
            raise ValueError("Evaluation inputs must match immutable mixture order")
        return self


class EvaluationDatasetGrant(TrainingWire):
    grant_id: uuid.UUID
    evaluation_attempt_id: uuid.UUID
    dataset_id: uuid.UUID
    node: StudioName
    source_sha256: Digest
    max_bytes: int = Field(strict=True, ge=1, le=256 * 1024**2)
    expires_at: AwareDatetime
    secret: str = Field(min_length=32, max_length=256, repr=False)


class EvaluationTrainingInputsRequest(TrainingWire):
    run_id: uuid.UUID
    request_sha256: Digest
    grants: list[EvaluationDatasetGrant] = Field(min_length=1, max_length=16)


def training_input_shape(binding: EvaluationTrainingBinding | None) -> dict[str, object] | None:
    if binding is None:
        return None
    return {
        "algorithm": "input-exact-v1",
        "seed": binding.seed,
        "samples_consumed": binding.completed_update
        * binding.batch_size
        * binding.accumulation_steps,
        "epoch_samples": binding.mixture.epoch_samples,
        "strategy": binding.mixture.mixture_strategy,
        "replacement": binding.mixture.replacement,
        "state_bytes": binding.state_bytes,
        "sources": [
            {
                "format": source.format.value,
                "source_sha256": source.source_sha256,
                "source_bytes": source.source_bytes,
                "split_bytes": source.split_bytes,
                "selected_pool": item.sample_count,
            }
            for source, item in zip(binding.sources, binding.mixture.datasets, strict=True)
        ],
    }


def training_scan_memory_bytes(binding: EvaluationTrainingBinding | None) -> int:
    """Conservative Python metadata/replay/JSON peak, reserved in addition to engines."""
    if binding is None:
        return 0
    return (
        720 * binding.mixture.epoch_samples
        + 256 * sum(item.sample_count for item in binding.mixture.datasets)
        + 24 * sum(source.split_bytes for source in binding.sources)
        + 4 * max(source.source_bytes for source in binding.sources)
        + 16 * 1024**2
    )
