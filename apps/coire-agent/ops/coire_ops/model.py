"""Gateway-backed Pydantic AI model with a structurally bounded ops toolset."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, PromptedOutput, RunContext
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from coire_core.models.console import ConsoleSnapshot
from coire_core.models.instance import TERMINAL_INSTANCE_STATES
from coire_core.models.ops import ResolvedOpsAction

OPS_TOOL_NAMES = frozenset({"read_snapshot", "propose_reversible_action"})
OPS_ANTHROPIC_MODEL = "claude-sonnet-5-5"


def _coire_gateway_profile(profile: ModelProfile) -> ModelProfile:
    """Keep the OpenAI token cap on the field Coire's gateway accepts."""

    adjusted: dict[str, object] = dict(cast(Mapping[str, object], profile))
    adjusted["openai_chat_supports_max_completion_tokens"] = False
    return cast(ModelProfile, adjusted)


def compact_snapshot(snapshot: ConsoleSnapshot) -> dict[str, object]:
    """Keep live actionable facts while omitting historical instance detail."""

    counts: dict[str, int] = {}
    active_instances: list[dict[str, object]] = []
    for instance in snapshot.cluster.instances:
        state = str(instance.effective_state)
        counts[state] = counts.get(state, 0) + 1
        if instance.effective_state not in TERMINAL_INSTANCE_STATES:
            active_instances.append(
                instance.model_dump(
                    mode="json",
                    include={
                        "id",
                        "model_id",
                        "variant_id",
                        "policy",
                        "state",
                        "effective_state",
                        "in_flight",
                        "updated_at",
                    },
                )
            )
    return {
        "observed_at": snapshot.observed_at.isoformat(),
        "core": snapshot.core.model_dump(mode="json") if snapshot.core else None,
        "nodes": [
            node.model_dump(
                mode="json",
                include={
                    "id",
                    "name",
                    "reachability",
                    "stale",
                    "cpu_percent",
                    "gpu_percent",
                    "thermal_state",
                    "memory_free_bytes",
                    "disk_free_bytes",
                    "health_reason",
                },
            )
            for node in snapshot.cluster.nodes
        ],
        "instance_counts": counts,
        "active_instances": active_instances,
        "ledgers": [
            ledger.model_dump(
                mode="json",
                include={"node_id", "node_name", "free_bytes", "health", "health_reason"},
            )
            for ledger in snapshot.ledgers
        ],
        "alerts": [alert.model_dump(mode="json") for alert in snapshot.alerts],
    }


class OpsModelAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=4000)


@dataclass
class OpsModelDeps:
    snapshot: ConsoleSnapshot
    action: ResolvedOpsAction | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class OpsModelTurn:
    answer: str
    action: ResolvedOpsAction | None = None
    rationale: str | None = None


class OpsModel:
    """Calls only Coire's authenticated gateway; it never accepts an engine address."""

    def __init__(
        self,
        *,
        gateway_url: str,
        token: str,
        model_id: str,
        model_source: str = "studio",
        ops_api_url: str = "http://coire-api:8000",
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_url = gateway_url.rstrip("/")
        if model_source not in {"studio", "anthropic"}:
            raise ValueError("unsupported ops model source")
        if model_source == "studio" and not base_url.endswith("/v1"):
            raise ValueError("gateway_url must name Coire's /v1 surface")
        if model_source == "anthropic" and model_id != OPS_ANTHROPIC_MODEL:
            raise ValueError("a pinned Sonnet model is required")
        if not model_id:
            raise ValueError("a pinned ops model id is required")
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._model_id = model_id
        self._timeout = timeout_s
        self._transport = transport
        self._model_source = model_source
        self._ops_api_url = ops_api_url.rstrip("/")
        self._ops_token = token
        model: AnthropicModel | OpenAIChatModel
        if model_source == "anthropic":
            model = AnthropicModel(
                OPS_ANTHROPIC_MODEL,
                provider=AnthropicProvider(
                    api_key=token,
                    base_url=f"{self._ops_api_url}/api/v1/internal/ops/anthropic",
                ),
            )
        else:
            provider = OpenAIProvider(
                base_url=base_url,
                api_key=token,
                http_client=httpx.AsyncClient(
                    headers=self._headers,
                    timeout=timeout_s,
                    transport=transport,
                ),
            )
            # Coire's compatible gateway accepts max_tokens. The OpenAI profile
            # otherwise renames that setting to max_completion_tokens.
            model = OpenAIChatModel(
                model_id,
                provider=provider,
                profile=_coire_gateway_profile,
            )

        async def read_snapshot(ctx: RunContext[OpsModelDeps]) -> dict[str, object]:
            """Read the bounded control-plane snapshot supplied for this turn."""

            return compact_snapshot(ctx.deps.snapshot)

        async def propose_reversible_action(
            ctx: RunContext[OpsModelDeps],
            action: ResolvedOpsAction,
            rationale: str,
        ) -> str:
            """Stage one exact allowlisted reversible action for human review."""

            ctx.deps.action = action
            ctx.deps.rationale = rationale[:1000]
            return "Proposal staged for human confirmation; no mutation has executed."

        self.agent: Agent[OpsModelDeps, OpsModelAnswer] = Agent(
            model,
            deps_type=OpsModelDeps,
            output_type=PromptedOutput(OpsModelAnswer),
            instructions=(
                "Answer only from read_snapshot facts. Use propose_reversible_action only for "
                "one exact reversible operation. Never claim an action executed. If no reviewed "
                "operation fits, plainly refuse it."
            ),
            tools=[read_snapshot, propose_reversible_action],
            retries=2,
            name="coire-ops",
        )

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.agent._function_toolset.tools.values())

    async def healthy(self) -> bool:
        try:
            if self._model_source == "anthropic":
                async with httpx.AsyncClient(
                    base_url=f"{self._ops_api_url}/api/v1/internal/ops/anthropic",
                    headers={
                        "x-api-key": self._ops_token,
                        "anthropic-version": "2023-06-01",
                    },
                    timeout=min(self._timeout, 10.0),
                    transport=self._transport,
                ) as client:
                    response = await client.get(f"/v1/models/{OPS_ANTHROPIC_MODEL}")
                    response.raise_for_status()
                    model_info = response.json()
                    return (
                        isinstance(model_info, dict) and model_info.get("id") == OPS_ANTHROPIC_MODEL
                    )
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=min(self._timeout, 10.0),
                transport=self._transport,
            ) as client:
                response = await client.get("/models")
                response.raise_for_status()
                models = response.json().get("data", [])
        except (httpx.HTTPError, TypeError, ValueError):
            return False
        return any(
            item.get("id") == self._model_id and item.get("coire_load_state") == "loaded"
            for item in models
            if isinstance(item, dict)
        )

    async def run(self, *, question: str, snapshot: ConsoleSnapshot) -> OpsModelTurn:
        deps = OpsModelDeps(snapshot=snapshot)
        result = await self.agent.run(question, deps=deps, model_settings={"max_tokens": 512})
        return OpsModelTurn(
            answer=result.output.answer,
            action=deps.action,
            rationale=deps.rationale,
        )
