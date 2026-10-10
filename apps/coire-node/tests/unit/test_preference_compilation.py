"""Frozen paired source compilation validates identity and aggregate cache bounds."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_preference_analysis_unit import Tokenizer
from test_training_mixture_worker import frozen_sources

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import DatasetFormat
from coire_core.models.preference import PreferenceRow, PreferenceSplitManifest
from coire_core.models.training import ResolvedTrainingSpecV3
from coire_core.models.training_node import DatasetAnalysisWorkerInput, TrainingPrepareRequest
from coire_core.preference_data import preference_split_digest
from coire_node.training import rendering
from coire_node.training.datasets import analyze_source
from coire_node.training.preference_data import (
    PreferenceFrozenInputs,
    PreferenceSampler,
    compile_preference_samples,
)
from coire_node.training.worker import payload_sha256


def frozen(tmp_path: Path) -> tuple[TrainingPrepareRequest, list[PreferenceFrozenInputs]]:
    original, old_sources = frozen_sources(tmp_path)
    inputs = []
    resolved = original.resolved.model_dump(mode="json")
    spec = resolved["spec"]
    spec.update(
        schema_version=3, objective="dpo", objective_options={"beta": 0.1}, init_adapter=None
    )
    spec["data"]["loss_policy"] = "final_assistant"
    spec["optim"]["max_sequence_length"] = 64
    resolved["datasets"] = []
    for index, item in enumerate(old_sources):
        rows = [
            PreferenceRow.model_validate(
                {
                    "prompt": [{"role": "user", "content": f"source {index} prompt {number}"}],
                    "chosen": "yes",
                    "rejected": "no",
                }
            )
            for number in range(6)
        ]
        data = b"".join(json.dumps(row.model_dump(mode="json")).encode() + b"\n" for row in rows)
        item.source.write_bytes(data)
        item.source.chmod(0o600)
        digest = hashlib.sha256(data).hexdigest()
        split = PreferenceSplitManifest(
            dataset_id=item.binding.dataset_id,
            source_sha256=digest,
            seed=0,
            train_rows=[1, 2, 3, 4],
            validation_rows=[5, 6],
            row_content_sha256=[row.content_sha256() for row in rows],
            prompt_group_sha256=[row.prompt_sha256() for row in rows],
        )
        binding = item.binding.model_copy(
            update={
                "format": DatasetFormat.PREFERENCE,
                "source_sha256": digest,
                "split_sha256": preference_split_digest(split),
            }
        )
        analysis = analyze_source(
            item.source,
            DatasetAnalysisWorkerInput(
                command_id=uuid.uuid4(),
                analysis_id=uuid.uuid4(),
                binding=binding,
                source_bytes=len(data),
                memory_bytes=1024**2,
                deadline=datetime.now(UTC) + timedelta(minutes=1),
            ),
            Tokenizer(),
            tokenizer_sha256=resolved["tokenizer_sha256"],
            template_sha256=resolved["template_sha256"],
            runtime_sha256=resolved["runtime_sha256"],
            max_sequence_length=64,
        )
        assert analysis.state == "succeeded"
        inputs.append(PreferenceFrozenInputs(binding, split, analysis, item.source))
        resolved["datasets"].append(
            {
                "dataset_id": str(binding.dataset_id),
                "analysis_id": str(analysis.id),
                "source_sha256": digest,
                "split_sha256": binding.split_sha256,
                "analysis_sha256": payload_sha256(analysis),
            }
        )
    initial = {**spec["model"], "base_manifest_sha256": resolved["base_manifest_sha256"]}
    resolved.update(
        initial_target=initial, reference_target=initial, sampler_version="coire-pair-sampler-v1"
    )
    resolved["resource_envelope"].update(reference_weight_bytes=1, reference_adapter_bytes=0)
    return original.model_copy(
        update={"resolved": ResolvedTrainingSpecV3.model_validate(resolved)}
    ), inputs


def test_exact_source_compilation_keeps_pairs_masks_and_deterministic_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, inputs = frozen(tmp_path)
    training, validation = compile_preference_samples(command, inputs, Tokenizer())
    assert isinstance(training, PreferenceSampler) and isinstance(validation, PreferenceSampler)
    batch = training.next_batch()
    assert len(batch.chosen_tokens) == 2 and sum(map(sum, batch.chosen_masks)) == 8
    assert sum(map(sum, batch.rejected_masks)) == 6
    state = training.snapshot()
    rebuilt, _ = compile_preference_samples(command, inputs, Tokenizer())
    rebuilt.restore(state)
    assert rebuilt.next_batch() == training.next_batch()
    assert len(validation.dataset) == 4


def test_changed_source_or_analysis_is_refused_without_native_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, inputs = frozen(tmp_path)
    inputs[0].source.write_bytes(b"changed")
    with pytest.raises(TrainingConflict):
        compile_preference_samples(command, inputs, Tokenizer())


def test_paired_cache_and_either_response_cannot_exceed_frozen_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, inputs = frozen(tmp_path)
    assert isinstance(command.resolved, ResolvedTrainingSpecV3)
    command.resolved.resource_envelope.buffer_bytes = 1
    with pytest.raises(TrainingValidationError, match="buffer"):
        compile_preference_samples(command, inputs, Tokenizer())


def test_stale_analysis_digest_and_either_side_overlength_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    monkeypatch.setattr(rendering, "_prepare_messages", lambda messages: None)
    command, inputs = frozen(tmp_path)
    bad = replace(
        inputs[0], analysis=inputs[0].analysis.model_copy(update={"runtime_sha256": "c" * 64})
    )
    with pytest.raises(TrainingConflict):
        compile_preference_samples(command, [bad, inputs[1]], Tokenizer())
    assert isinstance(command.resolved, ResolvedTrainingSpecV3)
    command.resolved.spec.optim.max_sequence_length = 8
    with pytest.raises(TrainingValidationError, match="sequence length"):
        compile_preference_samples(command, inputs, Tokenizer())
