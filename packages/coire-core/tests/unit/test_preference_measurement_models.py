"""Preference measurements require exact objective-specific evidence and bindings."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_preference_node_models import resolved

from coire_core.models.training import (
    PreferenceMemoryEvidence,
    ResolvedTrainingSpecV3,
    TrainingMeasurementRequest,
    TrainingMeasurementResult,
    TrainingMeasurementWorkload,
    TrainingMemoryEvidence,
    TrainingNodeMemoryEvidence,
)


def evidence() -> PreferenceMemoryEvidence:
    intent = ResolvedTrainingSpecV3.model_validate(resolved())
    return PreferenceMemoryEvidence(
        measurement_id=uuid.uuid4(),
        training_config_sha256="a" * 64,
        base_manifest_sha256=intent.base_manifest_sha256,
        tokenizer_sha256=intent.tokenizer_sha256,
        template_sha256=intent.template_sha256,
        runtime_sha256=intent.runtime_sha256,
        worker_version="1",
        completed_updates=4,
        resource_envelope=intent.resource_envelope,
        nodes=[
            TrainingNodeMemoryEvidence(
                node="coire-edge-a",
                hardware_sha256="b" * 64,
                peak_footprint_bytes=1,
                swap_growth_bytes=0,
                thermal_ok=True,
            )
        ],
        measured_at=datetime.now(UTC),
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        objective="dpo",
        objective_options=intent.spec.objective_options,
        initial_target=intent.initial_target,
        reference_target=intent.reference_target,
        datasets=intent.datasets,
    )


def test_preference_evidence_roundtrip_cannot_alias_sft() -> None:
    value = evidence()
    assert PreferenceMemoryEvidence.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        TrainingMemoryEvidence.model_validate(value.model_dump())
    data = value.model_dump(mode="json")
    data["reference_target"] = None
    with pytest.raises(ValidationError):
        PreferenceMemoryEvidence.model_validate(data)
    data = value.model_dump(mode="json")
    data["resource_envelope"]["reference_weight_bytes"] = 0
    with pytest.raises(ValidationError):
        PreferenceMemoryEvidence.model_validate(data)


def test_measurement_result_rejects_missing_evidence_or_changed_objective() -> None:
    intent = ResolvedTrainingSpecV3.model_validate(resolved())
    request = TrainingMeasurementRequest(
        spec=intent.spec,
        nodes=["coire-edge-a"],
        workload=TrainingMeasurementWorkload(
            sha256="e" * 64, concurrency_per_target=1, arrival_interval_ms=1, max_output_tokens=1
        ),
        mode="memory",
    )
    sample = evidence()
    data = {
        "id": sample.measurement_id,
        "request": request,
        "state": "succeeded",
        "report_sha256": "d" * 64,
        "completed_updates": 4,
        "thermal_ok": True,
        "created_at": datetime.now(UTC),
        "memory_evidence": sample,
    }
    assert TrainingMeasurementResult.model_validate(data).memory_evidence == sample
    with pytest.raises(ValidationError):
        TrainingMeasurementResult.model_validate({**data, "memory_evidence": None})
    changed = sample.model_dump(mode="json")
    changed.update(objective="orpo", objective_options={"weight": 0.1}, reference_target=None)
    changed["resource_envelope"].update(reference_weight_bytes=0, reference_adapter_bytes=0)
    with pytest.raises(ValidationError):
        TrainingMeasurementResult.model_validate({**data, "memory_evidence": changed})


def test_native_observation_binds_actual_reference_and_every_update_probe() -> None:
    from training_measurement_fixtures import experiment, observation

    from coire_core.models.training_node import (
        PreferenceMeasurementObservation,
        TrainingMeasurementObservation,
        parse_training_measurement_observation,
    )

    sample = evidence()
    old = observation(experiment()[1].commands[0])
    payload = {
        **old.model_dump(),
        "schema_version": 3,
        "objective": "dpo",
        "initial_target": sample.initial_target,
        "reference_target": sample.reference_target,
        "reference_weight_bytes": 1024,
        "reference_adapter_bytes": 0,
        "probe_count": old.completed_updates,
    }
    native = PreferenceMeasurementObservation.model_validate(payload)
    assert parse_training_measurement_observation(native.model_dump()) == native
    assert parse_training_measurement_observation(old.model_dump()) == old
    with pytest.raises(ValidationError):
        TrainingMeasurementObservation.model_validate(payload)
    for change in ({"reference_weight_bytes": 0}, {"probe_count": 1}, {"objective": "orpo"}):
        with pytest.raises(ValidationError):
            PreferenceMeasurementObservation.model_validate({**payload, **change})
