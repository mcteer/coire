"""The three MCP tools, backed by the ordinary durable Studio run lifecycle."""

from __future__ import annotations

import asyncio
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select

from coire_api import mcp_calls, runs
from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, bound_principal
from coire_api.coding_calls import request_coding_kill
from coire_api.db import (
    AgentRunRow,
    McpCallRow,
    ModelRow,
    ModelVariantRow,
    session_scope,
)
from coire_api.registry.service import is_chat_backend
from coire_core.errors import ChatNotFound
from coire_core.models.harness import PROFILE_MODEL_TAGS, ProfileName, TaskClass
from coire_core.models.mcp import (
    ApplyInput,
    ApplyResult,
    McpCallState,
    McpToolName,
    PlanInput,
    PlanResult,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)
from coire_core.models.registry import ModelState, Visibility
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRunCreate,
    AgentRunState,
    RunLimits,
)
from coire_core.settings import get_settings
from coire_mcp.telemetry import call_duration, calls_total, tracer


def _same_repository(left: WorkspaceSource, right: WorkspaceSource) -> bool:
    return left.repository_url == right.repository_url and left.workspace_id == right.workspace_id


async def _choose_model(
    principal: Principal, requested: uuid.UUID | None, task_class: TaskClass
) -> uuid.UUID:
    async with session_scope() as session:
        models = list(
            (
                await session.scalars(select(ModelRow).where(ModelRow.state == ModelState.READY))
            ).all()
        )
        eligible = []
        for model in models:
            if not is_chat_backend(model):
                continue
            if requested is not None and model.id != requested:
                continue
            if model.visibility is not Visibility.PUBLISHED and not principal.is_admin:
                continue
            if (
                not set(model.entitlement).issubset(principal.entitlements)
                and not principal.is_admin
            ):
                continue
            if not set(model.tags).intersection(PROFILE_MODEL_TAGS[ProfileName.CODING]):
                continue
            variant = await session.scalar(
                select(ModelVariantRow.id)
                .where(ModelVariantRow.model_id == model.id, *runs.variant_gate(task_class))
                .limit(1)
            )
            if variant is not None:
                eligible.append(model.id)
        if not eligible:
            gate = "harness-verified" if task_class is TaskClass.WRITE else "published validated"
            raise ValueError(f"no entitled {gate} coding model is available")
        return sorted(eligible, key=str)[0]


async def _kill_owned_run(run_id: uuid.UUID, principal: Principal, reason: str) -> None:
    async with session_scope() as session:
        try:
            await request_coding_kill(
                session, principal, run_id, reason=reason, audit_action="mcp_run.kill"
            )
        except ChatNotFound:
            return


async def _execute(
    tool: McpToolName,
    payload: ResearchInput | PlanInput | ApplyInput,
    task: str,
) -> dict[str, object]:
    principal = bound_principal()
    owner_id = mcp_calls.require_mcp_owner(principal)
    task_class = TaskClass.WRITE if tool is McpToolName.APPLY else TaskClass.READ
    try:
        model_id = await _choose_model(principal, payload.model_id, task_class)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    started = asyncio.get_running_loop().time()
    run_id: uuid.UUID | None = None
    call_id: uuid.UUID | None = None
    terminal_state: AgentRunState | None = None
    terminal_failure_code: str | None = None
    with tracer.start_as_current_span(f"coire.mcp.{tool.value}") as span:
        span.set_attribute("user_id", str(owner_id))
        span.set_attribute("model_id", str(model_id))
        try:
            async with session_scope() as session:
                call = await mcp_calls.create_call(
                    session,
                    principal=principal,
                    tool=tool,
                    source=payload.source,
                    model_id=model_id,
                    input=payload,
                    task=task,
                )
                call_id = call.id
                run = await runs.create_run(
                    session,
                    AgentRunCreate(
                        profile=ProfileName.CODING,
                        primary_model_id=model_id,
                        workspace_ref=f"pending-{call.id.hex}",
                        task_class=task_class,
                        prepared_request_id=call.id,
                        permitted_model_ids=frozenset({model_id}),
                        limits=RunLimits(timeout_seconds=get_settings().mcp_run_timeout_seconds),
                    ),
                    requester_user_id=owner_id,
                )
                run_id = run.id
                await write_principal_audit(
                    session,
                    principal=principal,
                    action=f"mcp.{tool.value}",
                    target_type="mcp_call",
                    target_id=str(call.id),
                    detail={"run_id": str(run.id), "model_id": str(model_id)},
                )
            span.set_attribute("run_id", str(run_id))
            while True:
                if asyncio.get_running_loop().time() - started > 1200:
                    terminal_state = AgentRunState.TIMED_OUT
                    terminal_failure_code = "mcp_call_timeout"
                    raise TimeoutError("MCP coding call exceeded its 20 minute budget")
                async with session_scope() as session:
                    row = await session.get(AgentRunRow, run_id)
                    if row is None:
                        raise RuntimeError("MCP run disappeared")
                    if row.state in TERMINAL_RUN_STATES:
                        if row.state is not AgentRunState.SUCCEEDED or row.result is None:
                            terminal_state = row.state
                            terminal_failure_code = row.failure_code
                            raise RuntimeError(
                                f"MCP run {row.state.value}: {row.failure_code or 'no result'}"
                            )
                        output = row.result.get("output")
                        if not isinstance(output, dict):
                            raise RuntimeError("MCP run result has no structured output")
                        result: ResearchResult | PlanResult | ApplyResult
                        if tool is McpToolName.RESEARCH:
                            result = ResearchResult.model_validate(output)
                        elif tool is McpToolName.PLAN:
                            result = PlanResult.model_validate(output)
                        else:
                            result = ApplyResult.model_validate(output)
                        stored_call = await session.get(McpCallRow, call_id, with_for_update=True)
                        if stored_call is None:
                            raise RuntimeError("MCP call disappeared")
                        await mcp_calls.store_result(session, row=stored_call, result=result)
                        calls_total.add(1, {"tool": tool.value, "outcome": "succeeded"})
                        return result.model_dump(mode="json")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            if run_id is not None:
                await asyncio.shield(_kill_owned_run(run_id, principal, "MCP client disconnected"))
            if call_id is not None:
                async with session_scope() as session:
                    cancelled_call = await session.get(McpCallRow, call_id, with_for_update=True)
                    if cancelled_call is not None and cancelled_call.state not in {
                        McpCallState.SUCCEEDED,
                        McpCallState.FAILED,
                        McpCallState.TIMED_OUT,
                        McpCallState.CANCELLED,
                    }:
                        await mcp_calls.fail_call(
                            session,
                            row=cancelled_call,
                            state=McpCallState.CANCELLED,
                            code="client_disconnected",
                        )
            calls_total.add(1, {"tool": tool.value, "outcome": "cancelled"})
            raise
        except Exception:
            if run_id is not None:
                await _kill_owned_run(run_id, principal, "MCP call failed")
            if call_id is not None:
                async with session_scope() as session:
                    failed_call = await session.get(McpCallRow, call_id, with_for_update=True)
                    if failed_call is not None and failed_call.state not in {
                        McpCallState.SUCCEEDED,
                        McpCallState.FAILED,
                        McpCallState.TIMED_OUT,
                        McpCallState.CANCELLED,
                    }:
                        state = (
                            McpCallState.TIMED_OUT
                            if terminal_state is AgentRunState.TIMED_OUT
                            else McpCallState.CANCELLED
                            if terminal_state is AgentRunState.KILLED
                            else McpCallState.FAILED
                        )
                        await mcp_calls.fail_call(
                            session,
                            row=failed_call,
                            state=state,
                            code=terminal_failure_code or "mcp_run_failed",
                        )
            metric_outcome = (
                "timed_out"
                if terminal_state is AgentRunState.TIMED_OUT
                else "cancelled"
                if terminal_state is AgentRunState.KILLED
                else "failed"
            )
            calls_total.add(1, {"tool": tool.value, "outcome": metric_outcome})
            raise
        finally:
            call_duration.record(asyncio.get_running_loop().time() - started, {"tool": tool.value})


def register_tools(server: MCPServer[object]) -> None:
    @server.tool(
        name="research", description="Answer a repository question with file and line citations"
    )
    async def research(
        source: WorkspaceSource, question: str, model_id: uuid.UUID | None = None
    ) -> ResearchResult:
        input = ResearchInput(source=source, question=question, model_id=model_id)
        return ResearchResult.model_validate(
            await _execute(McpToolName.RESEARCH, input, input.question)
        )

    @server.tool(name="plan", description="Plan repository changes with acceptance criteria")
    async def plan(
        source: WorkspaceSource,
        goal: str,
        research_result_id: uuid.UUID | None = None,
        model_id: uuid.UUID | None = None,
    ) -> PlanResult:
        input = PlanInput(
            source=source,
            goal=goal,
            research_result_id=research_result_id,
            model_id=model_id,
        )
        principal = bound_principal()
        prior = ""
        if input.research_result_id is not None:
            async with session_scope() as session:
                prior_call = await mcp_calls.get_owned_call(
                    session, call_id=input.research_result_id, principal=principal
                )
                if not _same_repository(
                    WorkspaceSource.model_validate(prior_call.source), input.source
                ):
                    raise ValueError("research result belongs to another workspace source")
                result = ResearchResult.model_validate(
                    await mcp_calls.get_owned_result(
                        session,
                        call_id=input.research_result_id,
                        principal=principal,
                        expected_type=ResearchResult,
                    )
                )
                prior = f"\nPrior research: {result.model_dump_json()}"
                input = input.model_copy(
                    update={
                        "source": input.source.model_copy(
                            update={"revision": result.source_revision}
                        )
                    }
                )
        return PlanResult.model_validate(
            await _execute(McpToolName.PLAN, input, input.goal + prior)
        )

    @server.tool(name="apply", description="Commit changes on a new branch and report tests")
    async def apply(
        source: WorkspaceSource,
        plan_result_id: uuid.UUID | None = None,
        plan: str | None = None,
        model_id: uuid.UUID | None = None,
    ) -> ApplyResult:
        input = ApplyInput(
            source=source,
            plan_result_id=plan_result_id,
            plan=plan,
            model_id=model_id,
        )
        principal = bound_principal()
        plan_text = input.plan
        if input.plan_result_id is not None:
            async with session_scope() as session:
                prior_call = await mcp_calls.get_owned_call(
                    session, call_id=input.plan_result_id, principal=principal
                )
                if not _same_repository(
                    WorkspaceSource.model_validate(prior_call.source), input.source
                ):
                    raise ValueError("plan result belongs to another workspace source")
                result = PlanResult.model_validate(
                    await mcp_calls.get_owned_result(
                        session,
                        call_id=input.plan_result_id,
                        principal=principal,
                        expected_type=PlanResult,
                    )
                )
                plan_text = result.model_dump_json()
                input = input.model_copy(
                    update={
                        "source": input.source.model_copy(
                            update={"revision": result.source_revision}
                        )
                    }
                )
        assert plan_text is not None
        return ApplyResult.model_validate(await _execute(McpToolName.APPLY, input, plan_text))
