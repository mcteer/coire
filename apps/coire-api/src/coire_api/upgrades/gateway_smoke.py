"""Verify the actual API namespace before an operation-scoped serving smoke."""

from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from coire_api.auth import Principal
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreExecutorEpoch,
    CoreRuntimeSelection,
    DeploymentOperationHold,
    DeploymentOwnership,
    UpgradeExecutionPlan,
    UpgradeSmokeRequest,
    UpgradeSmokeResult,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def linux_container_platform() -> bool:
    return sys.platform == "linux"


def api_container_namespace() -> str:
    if not linux_container_platform() or platform.machine() not in {"arm64", "aarch64"}:
        raise UpgradeUnavailable("Gateway upgrade smoke requires the Core API container")
    return platform.node()


def verify_smoke_runtime(
    settings: Settings,
    plan: UpgradeExecutionPlan,
    request: UpgradeSmokeRequest,
    *,
    require_fresh: bool = True,
) -> CoreRuntimeSelection:
    """Use fresh independent Docker proof, never the executor client's local stage."""
    if request.generation != plan.operation.generation or request.check not in {
        "gateway",
        "source",
    }:
        raise UpgradeConflict("Core smoke differs from the accepted fixed check")
    release = plan.source if request.check == "source" else plan.target
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    owner = store.read("ownership.json", DeploymentOwnership)
    hold = store.read("operation-hold.json", DeploymentOperationHold)
    observed = store.read("observed-runtime.json", CoreRuntimeSelection)
    now = datetime.now(UTC)
    if (
        owner is None
        or hold is None
        or observed is None
        or owner.owner_kind != "executor"
        or owner.owner_identity != settings.upgrade_executor_id
        or (owner.project, owner.job_id, owner.operation_generation, owner.source_sha256)
        != (
            settings.upgrade_project,
            plan.operation.job_id,
            plan.operation.generation,
            plan.source.manifest_sha256,
        )
        or hold.released
        or hold.job_id != plan.operation.job_id
        or hold.generation != plan.operation.generation
        or hold.plan_sha256 != plan.authority_sha256()
        or (observed.project, observed.owner_identity, observed.generation)
        != (owner.project, owner.owner_identity, owner.generation)
        or (observed.release_id, observed.manifest_sha256, observed.compose_sha256)
        != (release.release_id, release.manifest_sha256, release.manifest.compose_sha256)
        or (require_fresh and not 0 <= (now - observed.selected_at).total_seconds() <= 5)
        or (
            require_fresh
            and any(
                not 0 <= (now - item.container.observed_at).total_seconds() <= 5
                for item in observed.services
            )
        )
        or {item.container.service: item.container.image_id for item in observed.services}
        != {role.service: role.image_id for role in release.manifest.services}
    ):
        raise UpgradeConflict("Gateway smoke lacks fresh exact accepted cohort observation")
    serving = next(
        (item.container for item in observed.services if item.container.service == "coire-api"),
        None,
    )
    if serving is None or api_container_namespace() != serving.container_id[:12]:
        raise UpgradeConflict("Gateway smoke reached a different API container")
    return observed


def smoke_deadline(
    settings: Settings, plan: UpgradeExecutionPlan, request: UpgradeSmokeRequest
) -> datetime:
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    epoch = store.read("observed-epoch.json", CoreExecutorEpoch)
    if epoch is None or (epoch.job_id, epoch.generation, epoch.plan_sha256) != (
        plan.operation.job_id,
        plan.operation.generation,
        plan.authority_sha256(),
    ):
        raise UpgradeConflict("Core serving smoke has no exact original executor budget")
    if request.check == "source":
        deadline = epoch.recovery_deadline
    else:
        if epoch.recovery_captured_at is not None or not epoch.service_windows:
            raise UpgradeConflict("Target serving smoke is outside forward runtime activation")
        last = next(
            (
                window
                for window in epoch.service_windows
                if window.service == plan.operation.cohort[-1]
            ),
            None,
        )
        if last is None:
            raise UpgradeConflict("Target serving smoke lacks the final service window")
        deadline = (
            min(last.deadline_at, epoch.outage_deadline)
            if epoch.outage_deadline
            else last.deadline_at
        )
    if deadline is None or not 0 < (deadline - datetime.now(UTC)).total_seconds() <= 300:
        raise UpgradeUnavailable("Core serving smoke original deadline is absent or expired")
    return deadline


async def run_gateway_smoke(
    job_id: str, body: UpgradeSmokeRequest, principal: Principal, settings: Settings
) -> UpgradeSmokeResult:
    """Serialize serving attempts, including requests whose response was lost."""
    from sqlalchemy import func, select

    from coire_api.db import session_scope

    async with session_scope() as session:
        acquired = await session.scalar(
            select(func.pg_try_advisory_xact_lock(func.hashtext(f"upgrade-serving:{job_id}")))
        )
        if not acquired:
            raise UpgradeConflict("The accepted operation already owns a serving smoke attempt")
        return await _run_gateway_smoke(job_id, body, principal, settings)


async def _run_gateway_smoke(
    job_id: str, body: UpgradeSmokeRequest, principal: Principal, settings: Settings
) -> UpgradeSmokeResult:
    """Generate only the accepted tiny asset through ordinary gateway ownership."""
    import asyncio
    import hashlib

    from coire_api.db import session_scope
    from coire_api.gateway.proxy import complete
    from coire_api.gateway.resolution import resolve_exact_target
    from coire_api.gateway.usage import UsageTracker
    from coire_api.upgrades.authorization import authorize
    from coire_api.upgrades.executor import scoped_operation
    from coire_api.upgrades.gateway_smoke_scope import gateway_smoke_scope
    from coire_api.upgrades.grants import initiating_actor
    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.gateway import GatewayProtocol, UsageOutcome
    from coire_core.models.upgrades import UpgradeOperation, UpgradePhase

    started = datetime.now(UTC)
    async with session_scope() as session:
        row, plan = await scoped_operation(session, job_id, principal, settings)
        current = UpgradeOperation.model_validate(row.payload)
        expected = (
            UpgradePhase.VERIFYING_SOURCE if body.check == "source" else UpgradePhase.VERIFYING
        )
        if current.phase is not expected or (body.check != "source" and current.cancel_requested):
            raise UpgradeConflict("Serving smoke is outside its accepted live stage")
        if body.check != "source":
            await authorize(session, initiating_actor(row))
        observed = verify_smoke_runtime(settings, plan, body)
        deadline = smoke_deadline(settings, plan, body)
        target = InferenceTarget(
            model_id=plan.model_id,
            variant_id=plan.model_variant_id,
            base_manifest_sha256=plan.model_manifest_sha256,
        )
        actor = initiating_actor(row)
    from coire_api.upgrades.smoke_instance import (
        cleanup_prior_smokes,
        cleanup_smoke_instance,
        prepare_smoke_instance,
        retain_smoke_result,
        retained_smoke_result,
        wait_smoke_instance,
    )

    cached = await retained_smoke_result(plan, body, deadline)
    if cached is not None:
        return cached
    await cleanup_prior_smokes(plan, settings, deadline, check=body.check)
    identity, scope = await prepare_smoke_instance(plan, body, principal, settings, deadline)
    try:
        await wait_smoke_instance(settings, identity, deadline)
        async with session_scope() as session:
            resolved = await resolve_exact_target(
                session, target, actor, instance_id=identity, upgrade_job_id=job_id
            )
            if (
                resolved.engine_id != scope.engine_id
                or resolved.instance_id != identity
                or resolved.engine_url is None
                or resolved.node != scope.node
                or resolved.target != target
            ):
                raise UpgradeUnavailable("Core serving smoke has no exact owned Studio resident")
    except BaseException:
        await asyncio.shield(cleanup_smoke_instance(settings, identity, scope, deadline))
        raise
    usage = UsageTracker(
        principal=principal, requested_model_id=str(plan.model_id), protocol=GatewayProtocol.OPENAI
    )
    usage.bind_resolution(resolved)
    outcome = UsageOutcome.FAILED
    try:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeUnavailable("Core serving smoke original deadline expired")
        payload: dict[str, object] = {
            "model": plan.model_slug,
            "messages": [
                {"role": "user", "content": "Count from one to sixty-four, one number per line."}
            ],
            "stream": False,
            "max_tokens": 64,
            "temperature": 0,
            "coire_upgrade_scope": scope.model_dump(mode="json"),
        }
        with gateway_smoke_scope(
            scope, principal, settings, resolved.instance_id, restored=body.check == "source"
        ):
            async with asyncio.timeout(remaining):
                response = await complete(resolved.engine_url, payload, settings)
        from coire_core.models.gateway import EngineChatSmokeResponse

        result = EngineChatSmokeResponse.model_validate(response)
        if not 10 <= result.usage.completion_tokens <= 64:
            raise UpgradeUnavailable("Core serving smoke lacks ten reported generated tokens")
        message = result.choices[0].message
        content = message.content
        if message.role != "assistant" or not isinstance(content, str) or not content:
            raise UpgradeUnavailable("Core serving smoke returned no generated text")
        # Recheck the serving API namespace and original budget after generation.
        # The owner is awaiting this response and cannot refresh its observation
        # while generation runs. Keep that original time: compare the exact
        # retained cohort/namespace, then let the executor physically reobserve
        # the full cohort before it emits serving evidence.
        fresh = verify_smoke_runtime(settings, plan, body, require_fresh=False)
        if fresh != observed or datetime.now(UTC) >= deadline:
            raise UpgradeConflict("Core serving identity changed during smoke")
        usage.prompt_tokens = result.usage.prompt_tokens
        usage.completion_tokens = result.usage.completion_tokens
        usage.reported_token_usage = True
        outcome = UsageOutcome.SUCCEEDED
        smoke_result = UpgradeSmokeResult(
            job_id=job_id,
            generation=plan.operation.generation,
            check=body.check,
            runtime_sha256=fresh.manifest_sha256,
            model_id=plan.model_id,
            model_variant_id=plan.model_variant_id,
            model_manifest_sha256=plan.model_manifest_sha256,
            generated_tokens=result.usage.completion_tokens,
            passed=True,
            started_at=started,
            finished_at=datetime.now(UTC),
            output_sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
    finally:
        try:
            await asyncio.shield(
                usage.finish(
                    outcome,
                    failure_code=None
                    if outcome is UsageOutcome.SUCCEEDED
                    else "upgrade_smoke_failed",
                )
            )
        finally:
            await asyncio.shield(cleanup_smoke_instance(settings, identity, scope, deadline))
    if (
        verify_smoke_runtime(settings, plan, body, require_fresh=False) != observed
        or datetime.now(UTC) >= deadline
    ):
        raise UpgradeConflict("Core serving identity or original budget changed during cleanup")
    smoke_result = smoke_result.model_copy(update={"finished_at": datetime.now(UTC)})
    await retain_smoke_result(identity, scope, smoke_result)
    return smoke_result
