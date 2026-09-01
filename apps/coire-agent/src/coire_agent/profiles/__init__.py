"""Small flat profile declarations; model choice remains registry tag driven."""

from coire_core.models.harness import (
    PROFILE_MODEL_TAGS,
    PROFILE_TOOL_NAMES,
    AgentProfile,
    ProfileName,
)

PROFILES: dict[ProfileName, AgentProfile] = {
    ProfileName.CODING: AgentProfile(
        name=ProfileName.CODING,
        system_prompt="Research first, make minimal verified code changes, and report tests.",
        output_type="coding_result",
        model_tags=list(PROFILE_MODEL_TAGS[ProfileName.CODING]),
        tool_names=sorted(PROFILE_TOOL_NAMES[ProfileName.CODING]),
        tool_packs=["git"],
        stop_sequences=["</tool_call>"],
        write_capable=True,
    ),
    ProfileName.GENERAL: AgentProfile(
        name=ProfileName.GENERAL,
        system_prompt="Answer accurately from supplied context and identify uncertainty.",
        output_type="general_result",
        model_tags=list(PROFILE_MODEL_TAGS[ProfileName.GENERAL]),
        tool_names=sorted(PROFILE_TOOL_NAMES[ProfileName.GENERAL]),
    ),
    ProfileName.IMAGE: AgentProfile(
        name=ProfileName.IMAGE,
        system_prompt="Produce a validated image-generation specification, never raw engine flags.",
        output_type="image_specification",
        model_tags=list(PROFILE_MODEL_TAGS[ProfileName.IMAGE]),
        tool_names=sorted(PROFILE_TOOL_NAMES[ProfileName.IMAGE]),
    ),
    ProfileName.OPS: AgentProfile(
        name=ProfileName.OPS,
        system_prompt="Diagnose Coire from read-only admin facts; mutations require confirmation.",
        output_type="ops_diagnosis",
        model_tags=list(PROFILE_MODEL_TAGS[ProfileName.OPS]),
        tool_names=sorted(PROFILE_TOOL_NAMES[ProfileName.OPS]),
    ),
}


def get_profile(name: ProfileName, *, allow_ops: bool = False) -> AgentProfile:
    if name is ProfileName.OPS and not allow_ops:
        raise PermissionError("ops profile is absent from the user harness")
    return PROFILES[name]
