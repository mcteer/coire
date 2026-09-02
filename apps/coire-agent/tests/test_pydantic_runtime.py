from __future__ import annotations

import pytest

from coire_agent.pydantic_runtime import build_agent
from coire_core.models.harness import ProfileName


@pytest.mark.parametrize("name", [ProfileName.CODING, ProfileName.GENERAL, ProfileName.IMAGE])
def test_user_profiles_construct_real_pydantic_ai_agents(name: ProfileName) -> None:
    assert build_agent(name).name == f"coire-{name.value}"


def test_ops_agent_requires_ops_distribution_boundary() -> None:
    with pytest.raises(PermissionError):
        build_agent(ProfileName.OPS)
    assert build_agent(ProfileName.OPS, allow_ops=True).name == "coire-ops"


def test_profile_selects_only_declared_tools() -> None:
    def search(query: str) -> object:
        return query

    def undeclared() -> object:
        return None

    agent = build_agent(ProfileName.GENERAL, tools={"search": search, "undeclared": undeclared})
    assert {tool.name for tool in agent._function_toolset.tools.values()} == {"search"}
