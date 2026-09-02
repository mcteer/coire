import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.harness import AgentProfile, ProfileName


def test_profile_rejects_more_than_ten_tools() -> None:
    with pytest.raises(ValidationError):
        AgentProfile(
            name=ProfileName.CODING,
            system_prompt="x",
            output_type="coding_result",
            model_tags=["coding"],
            tool_names=[f"tool_{index}" for index in range(11)],
        )


def test_profile_rejects_duplicate_tools() -> None:
    with pytest.raises(ValidationError):
        AgentProfile(
            name=ProfileName.GENERAL,
            system_prompt="x",
            output_type="general_result",
            model_tags=["general"],
            tool_names=["search", "search"],
        )


def test_uuid_is_wire_serializable() -> None:
    assert str(uuid.uuid4())
