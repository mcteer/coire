"""Preference checkpoint decisions retain negotiated version and kill precedence."""

from pathlib import Path

import pytest
from test_training_evaluation_control import decision

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import CheckpointCommitAcknowledgementV3
from coire_node.training.worker import checkpoint_decision_action


@pytest.mark.parametrize(
    "action,expected",
    [("continue", "pause"), ("pause", "pause"), ("kill", "kill"), ("cancel", "cancel")],
)
def test_v3_evaluation_ack_is_negotiated_and_kill_wins(action: str, expected: str) -> None:
    ack = CheckpointCommitAcknowledgementV3.model_validate(
        {**decision().model_dump(), "schema_version": 3}
    )
    assert checkpoint_decision_action(ack, action, spec_version=3) == expected
    with pytest.raises(TrainingConflict, match="negotiated"):
        checkpoint_decision_action(ack, action, spec_version=2)
    with pytest.raises(TrainingConflict, match="negotiated"):
        checkpoint_decision_action(decision(), action, spec_version=3)


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
def test_v3_prepare_lease_status_and_ack_refusal_are_authenticated_and_fenced(
    tmp_path: "Path", objective: str
) -> None:
    import threading
    import uuid
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient
    from preference_measurement_fixtures import preference_experiment
    from test_training_lifecycle import FakeProcesses, envelope, start

    from coire_core.models.node import NetworkPath
    from coire_core.models.training_node import (
        CheckpointCommitAcknowledgementV2,
        TrainingLeaseRenewal,
        TrainingPrepareRequest,
        TrainingStopRequest,
    )
    from coire_node.agent import create_app
    from coire_node.testing.harness import TOKEN, Agent
    from coire_node.training.journal import TrainingJournal
    from coire_node.training.supervisor import TrainingSupervisor

    _, dispatch, _ = preference_experiment("dpo" if objective == "dpo" else "orpo")
    command = dispatch.commands[0].prepare
    assert TrainingPrepareRequest.model_validate_json(command.model_dump_json()) == command
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    agent.settings.training_enabled = True
    journal = TrainingJournal(
        tmp_path / "attempts", node=command.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/v1/bin/python"),
        processes=FakeProcesses(),
        validate_ready=lambda _: None,
        memory_available=lambda: 32 * 1024**3,
        disk_available=lambda: 64 * 1024**3,
        disk_floor=0,
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    path = f"/node/training/attempts/{command.attempt_id}"
    try:
        control = create_app(
            agent.settings, agent.collector, listener=NetworkPath.CONTROL, training=supervisor
        )
        with TestClient(control) as client:
            assert (
                3
                in client.get("/node/health", headers=headers).json()["training_capabilities"][
                    "spec_versions"
                ]
            )
            assert (
                client.post(path + "/prepare", json=command.model_dump(mode="json")).status_code
                == 401
            )
            prepared = client.post(
                path + "/prepare", headers=headers, json=command.model_dump(mode="json")
            )
            assert prepared.status_code == 200 and prepared.json()["ready"], prepared.text
            assert (
                client.post(
                    path + "/prepare", headers=headers, json=command.model_dump(mode="json")
                ).json()
                == prepared.json()
            )
            assert (
                client.post(
                    path + "/start", headers=headers, json=start(command).model_dump(mode="json")
                ).status_code
                == 200
            )
            renewal = TrainingLeaseRenewal.model_validate(
                {
                    **envelope(command),
                    "command_id": uuid.uuid4(),
                    "lease_expires_at": datetime.now(UTC) + timedelta(seconds=30),
                }
            )
            lease_response = client.post(
                path + "/lease", headers=headers, json=renewal.model_dump(mode="json")
            )
            assert lease_response.status_code == 200, lease_response.text
            stale = renewal.model_copy(
                update={"command_id": uuid.uuid4(), "fence": command.fence + 1}
            )
            assert (
                client.post(
                    path + "/lease", headers=headers, json=stale.model_dump(mode="json")
                ).status_code
                == 409
            )
            ack = CheckpointCommitAcknowledgementV2.model_validate(
                {
                    **envelope(command),
                    "command_id": uuid.uuid4(),
                    "schema_version": 2,
                    "checkpoint_id": uuid.uuid4(),
                    "manifest_sha256": "a" * 64,
                    "update": 1,
                    "committed_update": 1,
                    "job_version": 1,
                }
            )
            assert (
                client.post(
                    path + "/checkpoint-commit", headers=headers, json=ack.model_dump(mode="json")
                ).status_code
                == 409
            )
            agent.settings.training_enabled = False
            assert client.get(path, headers=headers).json()["liveness"] == "running"
            stopped = TrainingStopRequest.model_validate(
                {**envelope(command), "command_id": uuid.uuid4(), "reason": "cancelled"}
            )
            assert client.post(
                path + "/stop", headers=headers, json=stopped.model_dump(mode="json")
            ).json()["stopped"]
        data = create_app(
            agent.settings, agent.collector, listener=NetworkPath.DATA, training=supervisor
        )
        with TestClient(data) as client:
            assert client.get(path, headers=headers).status_code == 404
    finally:
        journal.close()
        agent.close()
