"""Gateway-backed Pydantic AI model with a structurally bounded ops toolset."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from coire_core.models.console import ConsoleSnapshot
from coire_core.models.ops import ResolvedOpsAction

OPS_TOOL_NAMES = frozenset({"read_snapshot", "propose_reversible_action"})


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
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_url = gateway_url.rstrip("/")
        if not base_url.endswith("/v1"):
            raise ValueError("gateway_url must name Coire's /v1 surface")
        if not model_id:
            raise ValueError("a pinned ops model id is required")
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._model_id = model_id
        self._timeout = timeout_s
        self._transport = transport
        provider = OpenAIProvider(
            base_url=base_url,
            api_key=token,
            http_client=httpx.AsyncClient(
                headers=self._headers,
                timeout=timeout_s,
                transport=transport,
            ),
        )
        model = OpenAIChatModel(model_id, provider=provider)

        async def read_snapshot(ctx: RunContext[OpsModelDeps]) -> dict[str, object]:
            """Read the bounded control-plane snapshot supplied for this turn."""

            return ctx.deps.snapshot.model_dump(mode="json")

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
            output_type=OpsModelAnswer,
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
        return any(item.get("id") == self._model_id for item in models if isinstance(item, dict))

    async def run(self, *, question: str, snapshot: ConsoleSnapshot) -> OpsModelTurn:
        deps = OpsModelDeps(snapshot=snapshot)
        result = await self.agent.run(question, deps=deps)
        return OpsModelTurn(
            answer=result.output.answer,
            action=deps.action,
            rationale=deps.rationale,
        )
