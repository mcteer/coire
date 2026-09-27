"""Strict contracts for agent harness runs and capability evaluations."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coire_core.models.registry import (
    CapabilityProfile,
    StructuredOutput,
    ToolCalling,
)


class ProfileName(StrEnum):
    CODING = "coding"
    GENERAL = "general"
    IMAGE = "image"
    OPS = "ops"


PROFILE_TOOL_NAMES: dict[ProfileName, frozenset[str]] = {
    ProfileName.CODING: frozenset(
        {"read_file", "search", "apply_patch", "run_tests", "load_tool_pack"}
    ),
    ProfileName.GENERAL: frozenset({"search", "read_document", "load_tool_pack"}),
    ProfileName.IMAGE: frozenset({"inspect_image", "load_tool_pack"}),
    ProfileName.OPS: frozenset({"cluster_health", "list_models", "list_jobs", "load_tool_pack"}),
}

PROFILE_MODEL_TAGS: dict[ProfileName, tuple[str, ...]] = {
    ProfileName.CODING: ("coding", "reasoning"),
    ProfileName.GENERAL: ("general", "reasoning"),
    ProfileName.IMAGE: ("image", "vision"),
    ProfileName.OPS: ("reasoning", "general"),
}


class TaskClass(StrEnum):
    READ = "read"
    WRITE = "write"


class HarnessStrategy(StrEnum):
    NATIVE = "native"
    JSON = "json"
    DELIMITED = "delimited"


class EvaluationVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class HarnessMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(system|user|assistant|tool|summary)$")
    content: str = Field(max_length=2_000_000)
    tool_name: str | None = Field(default=None, max_length=64)
    truncated: bool = False


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    arguments: dict[str, Any]


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProfileName
    system_prompt: str = Field(min_length=1, max_length=16_384)
    output_type: str = Field(min_length=1, max_length=128)
    model_tags: list[str] = Field(min_length=1, max_length=8)
    tool_names: list[str] = Field(default_factory=list, max_length=10)
    tool_packs: list[str] = Field(default_factory=list, max_length=8)
    temperature: float = Field(default=0, ge=0, le=2)
    stop_sequences: list[str] = Field(default_factory=list, max_length=8)
    write_capable: bool = False

    @model_validator(mode="after")
    def unique_tools(self) -> AgentProfile:
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("tool names must be unique")
        return self


class HarnessRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProfileName
    variant_id: uuid.UUID
    task_class: TaskClass
    task: str = Field(min_length=1, max_length=100_000)
    history: list[HarnessMessage] = Field(default_factory=list, max_length=4096)
    capability_profile: CapabilityProfile
    context_window: int = Field(ge=256)
    thinking_token_limit: int = Field(default=0, ge=0)

    @property
    def tool_strategy(self) -> HarnessStrategy:
        return (
            HarnessStrategy.NATIVE
            if self.capability_profile.tool_calling is ToolCalling.NATIVE
            else HarnessStrategy.DELIMITED
        )

    @property
    def output_strategy(self) -> HarnessStrategy:
        if self.capability_profile.structured_output is StructuredOutput.JSON_SCHEMA:
            return HarnessStrategy.NATIVE
        if self.capability_profile.structured_output is StructuredOutput.JSON_MODE:
            return HarnessStrategy.JSON
        return HarnessStrategy.DELIMITED


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_limit: int = Field(ge=1)
    reported_prompt_tokens: int = Field(default=0, ge=0)
    transmitted_messages: int = Field(default=0, ge=0)
    summarized_messages: int = Field(default=0, ge=0)
    truncation_count: int = Field(default=0, ge=0)


class HarnessRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    profile: ProfileName
    variant_id: uuid.UUID
    output: dict[str, Any]
    reasoning: str | None = None
    retries: int = Field(default=0, ge=0)
    context: ContextBudget


class CategoryScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_calling: float = Field(ge=0, le=1)
    structured_output: float = Field(ge=0, le=1)
    edit_application: float = Field(ge=0, le=1)
    long_context: float = Field(ge=0, le=1)


class HarnessEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: uuid.UUID


class HarnessEvaluationTarget(HarnessEvaluationRequest):
    model_id: uuid.UUID
    capability_profile: CapabilityProfile


class HarnessEvaluationSubmission(HarnessEvaluationRequest):
    """Measured scorecard submitted by the isolated ops evaluator."""

    scores: CategoryScores
    verdict: EvaluationVerdict
    harness_version: str = Field(max_length=32)
    engine_version: str = Field(max_length=64)
    diagnostics: list[str] = Field(default_factory=list, max_length=32)


class HarnessEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    variant_id: uuid.UUID
    scores: CategoryScores
    overall_score: float = Field(ge=0, le=1)
    verdict: EvaluationVerdict
    harness_version: str = Field(max_length=32)
    engine_version: str = Field(max_length=64)
    diagnostics: list[str] = Field(default_factory=list, max_length=32)
    run_at: datetime
