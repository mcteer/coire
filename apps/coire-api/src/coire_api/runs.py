"""Authoritative AgentRun state and user/admin projections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from coire_api.auth import Principal
from coire_api.db import (
    AgentRunRow,
    AgentRunTransitionRow,
    EntitlementRow,
    McpCallRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAdapterRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.evaluations import target_is_write_verified, validate_target
from coire_core.models.acquisition import VariantState
from coire_core.models.adapters import InferenceTarget
from coire_core.models.auth import UserRole
from coire_core.models.harness import PROFILE_MODEL_TAGS, ProfileName, TaskClass
from coire_core.models.mcp import McpCallState, McpToolName
from coire_core.models.registry import ModelState, Visibility
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRun,
    AgentRunCreate,
    AgentRunState,
    RunLimits,
    RunOperation,
    RunResourceUsage,
    RunTokenScope,
)


class RunNotFound(LookupError):
    pass


class RunConflict(ValueError):
    pass


RUN_COMMAND_NAMESPACE = uuid.UUID("bb7f9712-318b-481d-99b6-8ec92d159c51")


def variant_gate(task_class: TaskClass) -> tuple[ColumnElement[bool], ...]:
    """Published and validated variants serve reads; writes need harness verification."""
    common = (ModelVariantRow.validated.is_(True), ModelVariantRow.published.is_(True))
    if task_class is TaskClass.WRITE:
        return (*common, ModelVariantRow.harness_verified.is_(True))
    return common


def run_command_id(run_id: uuid.UUID, operation: RunOperation, attempt: int = 1) -> uuid.UUID:
    return uuid.uuid5(RUN_COMMAND_NAMESPACE, f"{run_id}:{operation.value}:{attempt}")


async def project_run(session: AsyncSession, row: AgentRunRow) -> AgentRun:
    node = await session.get(NodeRow, row.node_id) if row.node_id else None
    mcp_call = (
        await session.get(McpCallRow, row.prepared_request_id)
        if row.prepared_request_id is not None
        else None
    )
    duration = (
        max(0.0, (row.finished_at - row.started_at).total_seconds())
        if row.finished_at is not None and row.started_at is not None
        else None
    )
    return AgentRun(
        id=row.id,
        requester_user_id=row.requester_user_id,
        profile=ProfileName(row.profile),
        primary_model_id=row.primary_model_id,
        primary_variant_id=row.primary_variant_id,
        primary_target=run_target(row),
        node_id=row.node_id,
        node_name=node.name if node else None,
        container_id=row.container_id,
        workspace_ref=row.workspace_ref,
        task_class=row.task_class or TaskClass.WRITE,
        mcp_tool=mcp_call.tool if mcp_call else None,
        mcp_outcome=mcp_call.state if mcp_call else None,
        duration_seconds=duration,
        output_ref=row.output_ref,
        state=row.state,
        limits=RunLimits.model_validate(row.limits),
        exit_code=row.exit_code,
        failure_code=row.failure_code,
        failure_detail=row.failure_detail,
        result=row.result,
        resource_usage=RunResourceUsage.model_validate(row.resource_usage or {}),
        requested_at=row.requested_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        killed_by=row.killed_by,
        killed_at=row.killed_at,
    )


async def create_run(
    session: AsyncSession, request: AgentRunCreate, *, requester_user_id: uuid.UUID
) -> AgentRunRow:
    mcp_call = None
    if request.prepared_request_id is not None:
        mcp_call = await session.get(McpCallRow, request.prepared_request_id, with_for_update=True)
        if (
            mcp_call is None
            or mcp_call.owner_user_id != requester_user_id
            or mcp_call.model_id != request.primary_model_id
            or mcp_call.state is not McpCallState.ACCEPTED
            or mcp_call.run_id is not None
            or request.profile is not ProfileName.CODING
            or request.task_class
            is not (TaskClass.WRITE if mcp_call.tool is McpToolName.APPLY else TaskClass.READ)
        ):
            raise RunConflict("MCP call is not eligible for this run")
        prepared_target = mcp_call.input.get("target")
        if (
            prepared_target is not None
            and InferenceTarget.model_validate(prepared_target) != request.primary_target
        ):
            raise RunConflict("MCP call exact target differs from run")
    user = await session.get(UserRow, requester_user_id)
    if user is None or not user.active:
        raise RunConflict("run requester is inactive")
    is_admin = user is not None and user.role is UserRole.ADMIN
    entitlements = set(
        (
            await session.scalars(
                select(EntitlementRow.name).where(
                    EntitlementRow.user_id == requester_user_id,
                    EntitlementRow.revoked_at.is_(None),
                )
            )
        ).all()
    )
    models = list(
        (
            await session.scalars(
                select(ModelRow).where(ModelRow.id.in_(request.permitted_model_ids))
            )
        ).all()
    )
    if len(models) != len(request.permitted_model_ids):
        raise RunConflict("every permitted model must be a registry model")
    for model in models:
        if model.state is not ModelState.READY:
            raise RunConflict("every permitted model must be ready")
        if not is_admin and (
            model.visibility is not Visibility.PUBLISHED
            or not set(model.entitlement).issubset(entitlements)
        ):
            raise RunConflict("permitted model is not available to requester")
    primary_model = next(model for model in models if model.id == request.primary_model_id)
    if not set(primary_model.tags).intersection(PROFILE_MODEL_TAGS[request.profile]):
        raise RunConflict("primary model is incompatible with the selected profile")
    exact_targets = request.permitted_targets
    if request.primary_target is not None and (
        request.primary_target.model_id != request.primary_model_id
        or request.primary_target not in exact_targets
    ):
        raise RunConflict("primary exact target must be permitted")
    if exact_targets:
        if {target.model_id for target in exact_targets} != set(request.permitted_model_ids):
            raise RunConflict("exact grants must cover every permitted model")
        if request.primary_target is None:
            raise RunConflict("exact grants require a primary target")
        for target in exact_targets:
            await authorize_run_target(session, target, request.task_class, is_admin, entitlements)
        variant = await session.get(ModelVariantRow, request.primary_target.variant_id)
        assert variant is not None
    else:
        variant = await _legacy_variant(session, request)
        # Freeze acquired base artifacts for new runs, including UUID-only callers.
        # Historical registries without manifest evidence retain base-only legacy scope.
        frozen: list[InferenceTarget] = []
        for model_id in sorted(request.permitted_model_ids, key=str):
            selected = (
                variant
                if model_id == request.primary_model_id
                else await _legacy_variant(
                    session, request.model_copy(update={"primary_model_id": model_id})
                )
            )
            digests = set(
                (
                    await session.scalars(
                        select(VariantCopyRow.manifest_sha256).where(
                            VariantCopyRow.variant_id == selected.id,
                            VariantCopyRow.verified.is_(True),
                        )
                    )
                ).all()
            )
            if not digests or digests == {None}:
                frozen = []
                break
            if len(digests) != 1 or None in digests:
                raise RunConflict("base manifest evidence is inconsistent")
            digest = next(iter(digests))
            assert digest is not None
            frozen.append(
                InferenceTarget(
                    model_id=model_id, variant_id=selected.id, base_manifest_sha256=digest
                )
            )
        if frozen:
            exact_targets = tuple(frozen)
            for target in exact_targets:
                await authorize_run_target(
                    session, target, request.task_class, is_admin, entitlements
                )
    scope = RunTokenScope(
        permitted_model_ids=request.permitted_model_ids,
        permitted_targets=exact_targets,
        permitted_tools=request.permitted_tools,
        spend_limit_tokens=request.spend_limit_tokens,
    )
    row = AgentRunRow(
        requester_user_id=requester_user_id,
        profile=request.profile.value,
        primary_model_id=request.primary_model_id,
        primary_variant_id=variant.id,
        primary_adapter_id=request.primary_target.adapter_id if request.primary_target else None,
        workspace_ref=request.workspace_ref,
        task_class=request.task_class,
        prepared_request_id=request.prepared_request_id,
        output_ref=request.output_ref,
        token_scope=scope.model_dump(mode="json"),
        state=AgentRunState.QUEUED,
        limits=request.limits.model_dump(mode="json"),
        resource_usage=RunResourceUsage().model_dump(mode="json"),
    )
    session.add(row)
    await session.flush()
    if mcp_call is not None:
        row.workspace_ref = f"mcp-{row.id.hex}"
        row.output_ref = f"mcp-out-{row.id.hex}"
        mcp_call.run_id = row.id
        mcp_call.state = McpCallState.QUEUED
    session.add(
        AgentRunTransitionRow(
            run_id=row.id, from_state=None, to_state=AgentRunState.QUEUED, reason="run requested"
        )
    )
    await session.flush()
    return row


async def authorize_run_target(
    session: AsyncSession,
    target: InferenceTarget,
    task_class: TaskClass,
    is_admin: bool,
    entitlements: set[str],
) -> None:
    try:
        await validate_target(session, target)
    except (ValueError, LookupError) as exc:
        raise RunConflict("exact run target is unavailable") from exc
    variant = await session.get(ModelVariantRow, target.variant_id)
    if (
        variant is None
        or not variant.validated
        or not variant.published
        or variant.state is not VariantState.READY
    ):
        raise RunConflict("exact run variant is unavailable")
    if target.adapter_id:
        adapter = await session.get(TrainingAdapterRow, target.adapter_id)
        if adapter is None or (
            not is_admin
            and (
                adapter.visibility != "published"
                or not set(adapter.required_entitlements).issubset(entitlements)
            )
        ):
            raise RunConflict("adapter is unavailable to requester")
    if task_class is TaskClass.WRITE and not await target_is_write_verified(session, target):
        raise RunConflict("exact target is not harness-verified")


def run_target(row: AgentRunRow) -> InferenceTarget | None:
    if not row.token_scope.get("permitted_targets"):
        return None
    scope = RunTokenScope.model_validate(row.token_scope)
    return next(
        (
            target
            for target in scope.permitted_targets
            if target.model_id == row.primary_model_id
            and target.variant_id == row.primary_variant_id
            and target.adapter_id == row.primary_adapter_id
        ),
        None,
    )


async def _legacy_variant(session: AsyncSession, request: AgentRunCreate) -> ModelVariantRow:
    eligible_model_ids = set(
        (
            await session.scalars(
                select(ModelVariantRow.model_id).where(
                    ModelVariantRow.model_id.in_(request.permitted_model_ids),
                    *variant_gate(request.task_class),
                )
            )
        ).all()
    )
    if eligible_model_ids != set(request.permitted_model_ids):
        needed = (
            "published harness-verified"
            if request.task_class is TaskClass.WRITE
            else "published validated"
        )
        raise RunConflict(f"every permitted model needs a {needed} variant")
    variant = await session.scalar(
        select(ModelVariantRow)
        .where(
            ModelVariantRow.model_id == request.primary_model_id,
            *variant_gate(request.task_class),
        )
        .order_by(ModelVariantRow.is_default.desc(), ModelVariantRow.updated_at.desc())
        .limit(1)
    )
    if variant is None:
        needed = (
            "published harness-verified"
            if request.task_class is TaskClass.WRITE
            else "published validated"
        )
        raise RunConflict(f"primary model has no {needed} variant")
    return variant


async def get_visible_run(
    session: AsyncSession, run_id: uuid.UUID, principal: Principal
) -> AgentRunRow:
    row = await session.get(AgentRunRow, run_id)
    if row is None or (not principal.is_admin and row.requester_user_id != principal.user_id):
        raise RunNotFound(str(run_id))
    return row


async def list_visible_runs(session: AsyncSession, principal: Principal) -> list[AgentRunRow]:
    query = select(AgentRunRow).order_by(AgentRunRow.requested_at.desc())
    if not principal.is_admin:
        if principal.user_id is None:
            return []
        query = query.where(AgentRunRow.requester_user_id == principal.user_id)
    return list((await session.scalars(query)).all())


async def transition(
    session: AsyncSession,
    row: AgentRunRow,
    state: AgentRunState,
    reason: str,
) -> None:
    if row.state in TERMINAL_RUN_STATES:
        raise RunConflict(f"run is terminal ({row.state.value})")
    if row.state is AgentRunState.KILL_REQUESTED and state is not AgentRunState.KILLED:
        raise RunConflict("run kill is pending")
    previous = row.state
    now = datetime.now(UTC)
    row.state = state
    row.updated_at = now
    if state is AgentRunState.RUNNING and row.started_at is None:
        row.started_at = now
    if state in TERMINAL_RUN_STATES:
        row.finished_at = now
    session.add(
        AgentRunTransitionRow(
            run_id=row.id,
            from_state=previous,
            to_state=state,
            reason=reason,
        )
    )
    await session.flush()
