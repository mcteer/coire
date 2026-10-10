"""V3 checkpoint identity remains explicit and independent of SFT state."""

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from coire_core.models.training import ResolvedTrainingSpecV3
from coire_core.models.training_node import CheckpointWorkerStateV3

FIXTURES = Path(__file__).resolve().parents[4] / "tests/fixtures/preference/legacy_training"


def resolved() -> dict[str, Any]:
    data = json.loads((FIXTURES / "resolved_v1.json").read_text())
    spec = data["spec"]
    spec.update(
        schema_version=3, objective="dpo", objective_options={"beta": 0.1}, init_adapter=None
    )
    initial = {**spec["model"], "base_manifest_sha256": data["base_manifest_sha256"]}
    data.update(
        initial_target=initial, reference_target=initial, sampler_version="coire-pair-sampler-v1"
    )
    data["resource_envelope"].update(reference_weight_bytes=1024, reference_adapter_bytes=0)
    return cast(dict[str, Any], data)


def test_reference_is_frozen_initial_policy() -> None:
    data = resolved()
    value = ResolvedTrainingSpecV3.model_validate(data)
    assert value.reference_target == value.initial_target
    data["reference_target"] = {**value.initial_target.model_dump(), "variant_id": uuid.uuid4()}
    with pytest.raises(ValidationError):
        ResolvedTrainingSpecV3.model_validate(data)


def test_orpo_has_no_reference_or_reference_allocation() -> None:
    data = resolved()
    data["spec"]["objective"] = "orpo"
    data["spec"]["objective_options"] = {"weight": 0.0}
    with pytest.raises(ValidationError):
        ResolvedTrainingSpecV3.model_validate(data)
    data["reference_target"] = None
    data["resource_envelope"]["reference_weight_bytes"] = 0
    assert ResolvedTrainingSpecV3.model_validate(data).reference_target is None


def test_resume_refuses_changed_reference_and_invalid_rng() -> None:
    intent = ResolvedTrainingSpecV3.model_validate(resolved())
    data = json.loads((FIXTURES / "worker_state.json").read_text())
    data.update(
        schema_version=3,
        objective="dpo",
        objective_options={"beta": 0.1},
        initial_target=intent.initial_target.model_dump(mode="json"),
        reference_target=intent.initial_target.model_dump(mode="json"),
        sampler={
            "dataset_sha256": "a" * 64,
            "seed": 0,
            "epoch": 0,
            "cursor": 0,
            "batch_size": intent.spec.optim.batch_size,
        },
    )
    value = CheckpointWorkerStateV3.model_validate(data)
    assert value.sampler.kind == "preference"
    data["mlx_rng_key"] = [-1, 0]
    with pytest.raises(ValidationError):
        CheckpointWorkerStateV3.model_validate(data)


def test_v3_acknowledgement_binds_negotiated_update() -> None:
    from datetime import UTC, datetime, timedelta

    from coire_core.models.training_node import (
        CheckpointCommitAcknowledgementV3,
        parse_checkpoint_acknowledgement,
    )

    data = {
        "schema_version": 3,
        "command_id": str(uuid.uuid4()),
        "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "attempt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "fence": 1,
        "request_sha256": "a" * 64,
        "node": "coire-edge-a",
        "rank": 0,
        "world_size": 1,
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1),
        "checkpoint_id": str(uuid.uuid4()),
        "manifest_sha256": "b" * 64,
        "update": 8,
        "committed_update": 8,
        "job_version": 2,
    }
    assert isinstance(parse_checkpoint_acknowledgement(data), CheckpointCommitAcknowledgementV3)
    with pytest.raises(ValidationError):
        parse_checkpoint_acknowledgement({**data, "committed_update": 16})
    with pytest.raises(ValidationError):
        parse_checkpoint_acknowledgement({**data, "schema_version": 4})


def test_worker_state_parser_preserves_legacy_and_rejects_unknown_versions() -> None:
    from coire_core.models.training_node import CheckpointWorkerState, parse_checkpoint_worker_state

    old = json.loads((FIXTURES / "worker_state.json").read_text())
    value = parse_checkpoint_worker_state(old)
    assert isinstance(value, CheckpointWorkerState)
    assert value.model_dump(mode="json") == old
    with pytest.raises(ValidationError):
        parse_checkpoint_worker_state({**old, "schema_version": 4})


def test_preference_progress_is_a_distinct_bounded_wire_shape() -> None:
    from datetime import UTC, datetime

    from coire_core.models.preference import PreferenceMetricSample
    from coire_core.models.training import PreferenceTrainingProgressEvent, TrainingEvent
    from coire_core.models.training_node import NodePreferenceProgressPayload, NodeTrainingEvent

    sample = PreferenceMetricSample(
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        fence=1,
        update=1,
        objective="dpo",
        kind="train",
        loss=0.5,
        pair_count=4,
        response_tokens=28,
        tokens_per_second=10,
        recorded_at=datetime.now(UTC),
    )
    native = NodeTrainingEvent(
        sequence=1,
        job_id=sample.job_id,
        attempt_id=sample.attempt_id,
        fence=1,
        update=1,
        recorded_at=sample.recorded_at,
        payload=NodePreferenceProgressPayload(metric=sample),
    )
    assert (
        NodeTrainingEvent.model_validate_json(native.model_dump_json()).payload.kind
        == "preference_progress"
    )
    public = TrainingEvent(
        id=1,
        job_id=sample.job_id,
        attempt_id=sample.attempt_id,
        state_version=1,
        occurred_at=sample.recorded_at,
        kind="preference_progress",
        payload=PreferenceTrainingProgressEvent(metric=sample),
    )
    assert TrainingEvent.model_validate_json(public.model_dump_json()) == public
    with pytest.raises(ValidationError):
        TrainingEvent.model_validate({**public.model_dump(), "job_id": sample.attempt_id})
