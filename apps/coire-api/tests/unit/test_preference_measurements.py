"""Preference profile assembly refuses incomplete or substituted native evidence."""

from typing import Literal

import pytest
from preference_measurement_fixtures import preference_experiment

from coire_core.errors import TrainingConflict
from coire_core.models.training import PreferenceMemoryEvidence
from coire_core.models.training_node import TrainingMeasurementObservation
from coire_core.settings import Settings
from coire_scheduler.training_measurements import build_report


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_objective_specific_report_counts_real_reference_and_isolation(
    objective: Literal["dpo", "orpo"],
) -> None:
    row, dispatch, native = preference_experiment(objective)
    report = build_report(row, dispatch, [native], [], Settings())
    evidence = report.memory_evidence
    assert isinstance(evidence, PreferenceMemoryEvidence)
    assert evidence.initial_target == native.initial_target
    assert evidence.reference_target == native.reference_target
    assert evidence.resource_envelope.reference_weight_bytes == native.reference_weight_bytes
    assert evidence.resource_envelope.memory_bytes > native.peak_footprint_bytes
    assert evidence.datasets == dispatch.commands[0].prepare.resolved.datasets
    old = TrainingMeasurementObservation.model_validate(
        native.model_dump(
            exclude={
                "schema_version",
                "objective",
                "implementation",
                "initial_target",
                "reference_target",
                "reference_weight_bytes",
                "reference_adapter_bytes",
                "probe_count",
            }
        )
    )
    with pytest.raises(TrainingConflict, match="Preference evidence"):
        build_report(row, dispatch, [old], [], Settings())


@pytest.mark.parametrize(
    "change",
    [
        {"probe_count": 1},
        {"objective": "orpo"},
        {"reference_target": None},
        {"request_sha256": "f" * 64},
    ],
)
def test_wrong_native_reference_objective_or_unobserved_probe_is_refused(
    change: dict[str, object],
) -> None:
    row, dispatch, native = preference_experiment()
    with pytest.raises(TrainingConflict):
        build_report(row, dispatch, [native.model_copy(update=change)], [], Settings())
