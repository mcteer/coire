"""Versioned acknowledgments preserve v1 bytes and bind evaluation pause decisions."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from coire_core.models.training_node import CheckpointCommitAcknowledgement


def ack() -> CheckpointCommitAcknowledgement:
    return CheckpointCommitAcknowledgement(
        command_id=uuid.uuid4(),
        job_id="01J00000000000000000000001",
        attempt_id="01J00000000000000000000002",
        fence=1,
        request_sha256="a" * 64,
        node="coire-edge-a",
        rank=0,
        world_size=1,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=20),
        checkpoint_id=uuid.uuid4(),
        manifest_sha256="b" * 64,
        update=8,
    )


def test_v1_acknowledgement_round_trip_has_no_new_defaults() -> None:
    from coire_core.models.training_node import parse_checkpoint_acknowledgement

    original = ack()
    assert (
        parse_checkpoint_acknowledgement(original.model_dump(mode="json")).model_dump_json()
        == original.model_dump_json()
    )
    assert original.schema_version == 1 and "evaluation_pause" not in original.model_dump()


def test_v2_pause_decision_binds_job_version_and_completed_update() -> None:
    from coire_core.models.training_node import (
        CheckpointCommitAcknowledgementV2,
        parse_checkpoint_acknowledgement,
    )

    original = ack()
    data = {
        **original.model_dump(mode="json"),
        "schema_version": 2,
        "job_version": 5,
        "committed_update": 8,
        "evaluation_pause": {
            "trigger_id": str(uuid.uuid4()),
            "pause_command_id": str(uuid.uuid4()),
            "pause_origin": "evaluation",
        },
    }
    parsed = parse_checkpoint_acknowledgement(data)
    assert isinstance(parsed, CheckpointCommitAcknowledgementV2)
    assert parsed.evaluation_pause is not None and parsed.job_version == 5
    for change in (
        {"committed_update": 16},
        {"job_version": 0},
        {"schema_version": 4},
        {"evaluation_pause": {**data["evaluation_pause"], "pause_origin": "admin"}},
    ):
        with pytest.raises(ValueError):
            parse_checkpoint_acknowledgement({**data, **change})
