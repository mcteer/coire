from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coire_core.models.harness import ProfileName
from coire_core.models.runs import (
    AgentRunCreate,
    AgentRunState,
    RunContainerCreate,
    RunLimits,
    RunTokenScope,
)


def test_run_create_requires_primary_model_in_scope_and_forbids_extra() -> None:
    primary = uuid.uuid4()
    with pytest.raises(ValidationError, match="primary_model_id"):
        AgentRunCreate(
            profile=ProfileName.CODING,
            primary_model_id=primary,
            workspace_ref="workspace-1",
            permitted_model_ids=frozenset({uuid.uuid4()}),
        )
    with pytest.raises(ValidationError):
        AgentRunCreate.model_validate(
            {
                "profile": "coding",
                "primary_model_id": primary,
                "workspace_ref": "workspace-1",
                "permitted_model_ids": [primary],
                "docker_socket": "/tmp/attacker.sock",
            }
        )


def test_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        RunLimits(memory_bytes=1)
    with pytest.raises(ValidationError):
        RunLimits(timeout_seconds=86_401)


def test_user_run_rejects_ops_and_tools_outside_profile() -> None:
    model_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="ops profile"):
        AgentRunCreate(
            profile=ProfileName.OPS,
            primary_model_id=model_id,
            workspace_ref="workspace",
            permitted_model_ids=frozenset({model_id}),
        )
    with pytest.raises(ValidationError, match="outside"):
        AgentRunCreate(
            profile=ProfileName.GENERAL,
            primary_model_id=model_id,
            workspace_ref="workspace",
            permitted_model_ids=frozenset({model_id}),
            permitted_tools=frozenset({"apply_patch"}),
        )


def test_node_create_requires_digest_and_hides_token_repr() -> None:
    command = RunContainerCreate(
        run_id=uuid.uuid4(),
        profile=ProfileName.GENERAL,
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        image=f"ghcr.io/mcteer/coire-agent@sha256:{'a' * 64}",
        argv=["-m", "coire_agent"],
        workspace_ref="workspace-1",
        run_token="secret-token-that-is-long-enough-to-be-valid",
        gateway_url="http://coire-core.lab:8080/v1",
        limits=RunLimits(),
    )
    assert "secret-token" not in repr(command)
    with pytest.raises(ValidationError):
        RunContainerCreate.model_validate({**command.model_dump(), "image": "coire-agent:latest"})


def test_scope_and_states_are_json_serializable() -> None:
    model_id = uuid.uuid4()
    scope = RunTokenScope(permitted_model_ids=frozenset({model_id}), spend_limit_tokens=100)
    assert scope.model_dump(mode="json")["permitted_model_ids"] == [str(model_id)]
    assert AgentRunState.KILLED.value == "killed"
    assert datetime.now(UTC).tzinfo is not None
