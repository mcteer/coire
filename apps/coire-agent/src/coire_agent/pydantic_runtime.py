"""Pydantic AI agent construction shared by user and ops distributions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from coire_agent.profiles import get_profile
from coire_core.models.harness import ProfileName


class CodingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    changed_files: list[str] = Field(default_factory=list)


class GeneralResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    uncertainty: str | None = None


class ImageSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class OpsDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding: str
    recommended_action: str


ProfileOutput = CodingResult | GeneralResult | ImageSpecification | OpsDiagnosis
OUTPUT_TYPES: dict[ProfileName, type[BaseModel]] = {
    ProfileName.CODING: CodingResult,
    ProfileName.GENERAL: GeneralResult,
    ProfileName.IMAGE: ImageSpecification,
    ProfileName.OPS: OpsDiagnosis,
}


def build_agent(
    profile_name: ProfileName,
    *,
    allow_ops: bool = False,
    tools: Mapping[str, Callable[..., object]] | None = None,
) -> Agent[None, ProfileOutput]:
    profile = get_profile(profile_name, allow_ops=allow_ops)
    supplied = tools or {}
    selected = [supplied[name] for name in profile.tool_names if name in supplied]
    agent = Agent(
        None,
        output_type=OUTPUT_TYPES[profile_name],
        system_prompt=profile.system_prompt,
        name=f"coire-{profile.name.value}",
        retries=2,
        defer_model_check=True,
        tools=selected,
    )
    return cast(Agent[None, ProfileOutput], agent)
