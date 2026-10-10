"""Synthetic preference control evidence; never a claim of native measurement."""

from typing import Literal

from training_measurement_fixtures import experiment, observation

from coire_api.db import TrainingMeasurementRow
from coire_api.training.service import payload_digest
from coire_core.models.datasets import DatasetFormat
from coire_core.models.preference import PreferenceAnalysis, PreferenceSplitManifest
from coire_core.models.training import ResolvedTrainingSpecV3, TrainingSpecV3
from coire_core.models.training_node import (
    PreferenceMeasurementObservation,
    TrainingMeasurementDispatch,
    TrainingMeasurementSource,
)
from coire_core.preference_data import preference_split_digest


def preference_experiment(
    objective: Literal["dpo", "orpo"] = "dpo",
) -> tuple[TrainingMeasurementRow, TrainingMeasurementDispatch, PreferenceMeasurementObservation]:
    row, old = experiment()
    source = old.sources[0]
    split = PreferenceSplitManifest.model_validate(
        {
            **source.split.model_dump(),
            "algorithm": "coire-preference-split-v1",
            "prompt_group_sha256": source.split.row_content_sha256,
        }
    )
    binding = source.binding.model_copy(
        update={"format": DatasetFormat.PREFERENCE, "split_sha256": preference_split_digest(split)}
    )
    summary = {"minimum": 4, "maximum": 4, "total": 8}
    response = {"minimum": 2, "maximum": 2, "total": 4}
    analysis = source.analysis.model_copy(
        update={
            "preference": PreferenceAnalysis.model_validate(
                {
                    "dataset_id": binding.dataset_id,
                    "source_sha256": binding.source_sha256,
                    "split_sha256": binding.split_sha256,
                    "tokenizer_sha256": source.analysis.tokenizer_sha256,
                    "template_sha256": source.analysis.template_sha256,
                    "runtime_sha256": source.analysis.runtime_sha256,
                    "row_count": 2,
                    "prompt_group_count": 2,
                    "token_rows_sha256": "a" * 64,
                    "chosen_tokens": summary,
                    "rejected_tokens": summary,
                    "chosen_response_tokens": response,
                    "rejected_response_tokens": response,
                }
            )
        }
    )
    paired = TrainingMeasurementSource(binding=binding, split=split, analysis=analysis)
    raw = old.commands[0].prepare.resolved.model_dump(mode="json")
    spec = raw["spec"]
    spec.update(
        schema_version=3,
        objective=objective,
        init_adapter=None,
        objective_options={"beta": 0.1} if objective == "dpo" else {"weight": 0.1},
    )
    spec["data"]["loss_policy"] = "final_assistant"
    spec["parameterization"]["dropout"] = 0
    target = {**spec["model"], "base_manifest_sha256": raw["base_manifest_sha256"]}
    raw.update(
        initial_target=target,
        reference_target=target if objective == "dpo" else None,
        sampler_version="coire-pair-sampler-v1",
    )
    raw["datasets"][0].update(
        split_sha256=binding.split_sha256, analysis_sha256=payload_digest(analysis)
    )
    raw["resource_envelope"].update(
        reference_weight_bytes=1 if objective == "dpo" else 0,
        reference_adapter_bytes=0,
        buffer_bytes=raw["resource_envelope"]["buffer_bytes"] - int(objective == "dpo"),
    )
    resolved = ResolvedTrainingSpecV3.model_validate(raw)
    probe = old.commands[0].model_copy(
        update={"prepare": old.commands[0].prepare.model_copy(update={"resolved": resolved})}
    )
    dispatch = old.model_copy(update={"commands": [probe], "sources": [paired]})
    row.request = {**row.request, "spec": resolved.spec.model_dump(mode="json")}
    observed = PreferenceMeasurementObservation.model_validate(
        {
            **observation(probe).model_dump(),
            "schema_version": 3,
            "objective": objective,
            "initial_target": resolved.initial_target,
            "reference_target": resolved.reference_target,
            "reference_weight_bytes": 32 * 1024**2 if objective == "dpo" else 0,
            "reference_adapter_bytes": 0,
            "probe_count": 2,
        }
    )
    assert isinstance(resolved.spec, TrainingSpecV3)
    return row, dispatch, observed
