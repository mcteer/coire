"""Small flat profile declarations; model choice remains registry tag driven."""

from coire_core.models.harness import AgentProfile, ProfileName

PROFILES: dict[ProfileName, AgentProfile] = {
    ProfileName.CODING: AgentProfile(
        name=ProfileName.CODING,
        system_prompt="Research first, make minimal verified code changes, and report tests.",
        output_type="coding_result",
        model_tags=["coding", "reasoning"],
        tool_names=["read_file", "search", "apply_patch", "run_tests", "load_tool_pack"],
        tool_packs=["git"],
        stop_sequences=["</tool_call>"],
        write_capable=True,
    ),
    ProfileName.GENERAL: AgentProfile(
        name=ProfileName.GENERAL,
        system_prompt="Answer accurately from supplied context and identify uncertainty.",
        output_type="general_result",
        model_tags=["general", "reasoning"],
        tool_names=["search", "read_document", "load_tool_pack"],
    ),
    ProfileName.IMAGE: AgentProfile(
        name=ProfileName.IMAGE,
        system_prompt="Produce a validated image-generation specification, never raw engine flags.",
        output_type="image_specification",
        model_tags=["image", "vision"],
        tool_names=["inspect_image", "load_tool_pack"],
    ),
    ProfileName.OPS: AgentProfile(
        name=ProfileName.OPS,
        system_prompt="Diagnose Coire from read-only admin facts; mutations require confirmation.",
        output_type="ops_diagnosis",
        model_tags=["reasoning", "general"],
        tool_names=["cluster_health", "list_models", "list_jobs", "load_tool_pack"],
    ),
}


def get_profile(name: ProfileName, *, allow_ops: bool = False) -> AgentProfile:
    if name is ProfileName.OPS and not allow_ops:
        raise PermissionError("ops profile is absent from the user harness")
    return PROFILES[name]
