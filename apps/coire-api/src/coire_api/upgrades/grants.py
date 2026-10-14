"""Fresh, phase-bound human authority with atomic activation intent persistence."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    UpgradeGrantRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UpgradeReceiptRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.compatibility import require_migration_backup
from coire_api.upgrades.persistence import append_receipt, get_operation, operation_lock
from coire_api.upgrades.schema_identity import inspect_schema
from coire_core.errors import UpgradeConflict
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    UpgradeActivationGrant,
    UpgradeActivationRequest,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeMaintenance,
    UpgradeMigrationExecution,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings, get_settings
from coire_core.upgrade_backups import verify_prepared_backup

if TYPE_CHECKING:
    from coire_api.upgrades.admission import CoreAdmissions


async def verify_fenced(session: AsyncSession, plan: UpgradeExecutionPlan) -> None:
    operation = plan.operation
    rows = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(
                UpgradeMaintenanceRow.target.in_(plan.maintenance.targets),
            )
        )
    ).all()
    if {row.target for row in rows} != set(plan.maintenance.targets):
        raise UpgradeConflict("Activation requires every accepted maintenance target")
    required_nodes = {
        target for target in plan.maintenance.targets if target in {"coire-edge-a", "coire-edge-b"}
    }
    for row in rows:
        value = UpgradeMaintenance.model_validate(row.payload)
        if (
            row.released
            or (row.job_id, row.generation) != (operation.job_id, operation.generation)
            or (value.job_id, value.generation) != (operation.job_id, operation.generation)
            or value.state != "active"
            or set(value.targets) != set(plan.maintenance.targets)
            or value.drain_deadline_at != plan.maintenance.drain_deadline_at
            or not required_nodes.issubset(value.acknowledged_generations)
        ):
            raise UpgradeConflict("Activation requires complete acknowledged drain ownership")


def initiating_actor(row: UpgradeOperationRow) -> Principal:
    # This snapshot identifies the original actor; authorize() locks and checks
    # their current account/key state before granting any new authority.
    return Principal(
        kind=PrincipalKind.API_KEY if row.key_id is not None else PrincipalKind.ADMIN,
        user_id=row.user_id,
        role=UserRole.ADMIN,
        scopes=frozenset({"admin"}),
        api_key_id=row.key_id,
        credential_version=row.credential_version,
    )


async def verify_activation_ready(
    session: AsyncSession, op: UpgradeOperation, boundary: str, plan: UpgradeExecutionPlan
) -> None:
    if op.authority_sha256() != plan.operation.authority_sha256():
        raise UpgradeConflict("Accepted immutable operation authority differs")
    if op.phase not in (UpgradePhase.STAGED, UpgradePhase.SMOKE_BEFORE, UpgradePhase.MIGRATING):
        raise UpgradeConflict("Operation is no longer at an activation boundary")
    if (
        op.phase is not UpgradePhase.MIGRATING
        and datetime.now(UTC) >= plan.maintenance.drain_deadline_at
    ):
        raise UpgradeConflict("Captured drain deadline elapsed before activation")
    proof_row = await session.scalar(
        select(UpgradeReceiptRow)
        .where(
            UpgradeReceiptRow.job_id == op.job_id,
            UpgradeReceiptRow.generation == op.generation,
        )
        .order_by(UpgradeReceiptRow.sequence.desc())
        .limit(1)
    )
    proof = UpgradeReceipt.model_validate(proof_row.payload) if proof_row is not None else None
    if proof is None or proof.outcome != "complete" or proof.observed_sha256 != op.source_sha256:
        raise UpgradeConflict("Activation requires current source verification evidence")
    if op.kind.value == "control_plane":
        if not proof.evidence:
            raise UpgradeConflict("Core activation requires verified prepared artifacts")
        evidence = UpgradeEvidence.model_validate_json(proof.evidence)
        changed_schema = plan.needs_migration
        if op.phase is UpgradePhase.STAGED:
            if (
                proof.phase is not UpgradePhase.STAGED
                or evidence.artifacts_verified is not True
                or evidence.configuration_verified is not True
                or evidence.release_sha256 != op.target_sha256
                or evidence.service_image_ids
                != {service.service: service.image_id for service in plan.target.manifest.services}
                or (boundary == "migration") != changed_schema
            ):
                raise UpgradeConflict("Prepared Core evidence or activation boundary differs")
            if changed_schema:
                require_migration_backup(plan)
            if changed_schema and (
                plan.backup is None
                or evidence.backup_validation_sha256 != plan.backup.validation_sha256
            ):
                raise UpgradeConflict("Migration requires the accepted restore-validated backup")
        elif (
            op.phase is not UpgradePhase.MIGRATING
            or proof.phase is not UpgradePhase.MIGRATING
            or boundary != "runtime"
            or not changed_schema
            or evidence.schema_head != plan.schema_head_after
            or evidence.migration_exit_code != 0
        ):
            raise UpgradeConflict("Runtime activation requires the expected completed schema")
    elif op.phase is UpgradePhase.SMOKE_BEFORE:
        if proof.phase is not UpgradePhase.SMOKE_BEFORE or not proof.evidence:
            raise UpgradeConflict("Activation requires completed pre-switch smoke")
        evidence = UpgradeEvidence.model_validate_json(proof.evidence)
        if (
            evidence.model_id != plan.model_id
            or evidence.model_variant_id != plan.model_variant_id
            or evidence.model_manifest_sha256 != plan.model_manifest_sha256
            or evidence.output_sha256 is None
            or evidence.runtime_sha256 is None
            or evidence.check != ("node" if op.kind.value == "studio" else "gateway")
            or evidence.cleanup_verified is not True
        ):
            raise UpgradeConflict("Smoke evidence differs from accepted registry asset")
        if (
            op.kind.value == "studio"
            and evidence.runtime_sha256 != plan.target.manifest.environment_sha256
        ):
            raise UpgradeConflict("Smoke evidence differs from exact candidate runtime")
        if evidence.generated_tokens is None or evidence.generated_tokens < 10:
            raise UpgradeConflict("Activation requires real tiny-model generation evidence")
    else:
        raise UpgradeConflict("Studio activation requires completed pre-switch smoke")


async def verify_current_schema(
    session: AsyncSession, operation: UpgradeOperation, plan: UpgradeExecutionPlan
) -> None:
    if operation.kind.value != "control_plane":
        return
    head, digest = await inspect_schema(session)
    if operation.phase is UpgradePhase.STAGED:
        if (head, digest) != (plan.initial_schema_head, plan.initial_schema_sha256):
            raise UpgradeConflict("Deployed schema changed after accepted preparation")
    else:
        row = await get_operation(session, operation.job_id, lock=True)
        if row.migration_execution is None:
            raise UpgradeConflict("Completed migration proof is unavailable")
        proof = UpgradeMigrationExecution.model_validate(row.migration_execution)
        if (
            proof.state != "complete"
            or proof.job_id != operation.job_id
            or proof.generation != operation.generation
            or proof.plan_sha256 != plan.authority_sha256()
            or (head, digest) != (proof.schema_after_head, proof.schema_after_sha256)
            or head != plan.schema_head_after
        ):
            raise UpgradeConflict("Deployed schema differs from completed migration")


async def issue(
    session: AsyncSession,
    job_id: str,
    request: UpgradeActivationRequest,
    *,
    settings: Settings | None = None,
    admissions: "CoreAdmissions | None" = None,
) -> UpgradeActivationGrant:
    await operation_lock(session)
    row = await get_operation(session, job_id, lock=True)
    op = UpgradeOperation.model_validate(row.payload)
    if (
        request.generation != op.generation
        or request.source_sha256 != op.source_sha256
        or op.finished_at is not None
        or op.cancel_requested
        or op.phase not in (UpgradePhase.STAGED, UpgradePhase.SMOKE_BEFORE, UpgradePhase.MIGRATING)
    ):
        raise UpgradeConflict("Activation boundary differs from current operation")
    if request.boundary == "migration" and op.kind.value != "control_plane":
        raise UpgradeConflict("Studio activation cannot request a schema migration")
    if row.execution_plan is None:
        raise UpgradeConflict("Accepted execution plan is unavailable")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    await verify_fenced(session, plan)
    await verify_activation_ready(session, op, request.boundary, plan)
    await verify_current_schema(session, op, plan)
    await verify_backup_boundary(plan, request.boundary, settings or get_settings())
    await verify_live_core_drain(session, plan, settings or get_settings(), admissions)
    actor = initiating_actor(row)
    await authorize(session, actor)
    existing = await session.scalar(
        select(UpgradeGrantRow)
        .where(
            UpgradeGrantRow.job_id == job_id,
            UpgradeGrantRow.generation == op.generation,
            UpgradeGrantRow.boundary == request.boundary,
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    if existing is not None:
        if existing.consumed_at is not None:
            raise UpgradeConflict("Activation authority was already consumed")
        existing.expires_at = now + timedelta(seconds=30)
        grant_row = existing
    else:
        grant_row = UpgradeGrantRow(
            id=uuid4(),
            job_id=job_id,
            generation=op.generation,
            boundary=request.boundary,
            expires_at=now + timedelta(seconds=30),
        )
        session.add(grant_row)
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.activation.authorize",
        target_type="upgrade",
        target_id=job_id,
        detail={"generation": op.generation, "boundary": request.boundary},
    )
    await session.flush()
    return UpgradeActivationGrant(
        job_id=job_id,
        generation=op.generation,
        boundary=request.boundary,
        grant_id=grant_row.id,
        expires_at=grant_row.expires_at,
    )


async def consume(
    session: AsyncSession,
    grant: UpgradeActivationGrant,
    intent: UpgradeReceipt,
    executor: Principal,
    *,
    settings: Settings | None = None,
    admissions: "CoreAdmissions | None" = None,
) -> None:
    await operation_lock(session)
    row = await get_operation(session, grant.job_id, lock=True)
    op = UpgradeOperation.model_validate(row.payload)
    authority = await session.get(UpgradeGrantRow, grant.grant_id, with_for_update=True)
    expected_phase = (
        UpgradePhase.MIGRATING if grant.boundary == "migration" else UpgradePhase.ACTIVATING
    )
    now = datetime.now(UTC)
    if authority is not None and authority.consumed_at is not None:
        retained = await session.get(
            UpgradeReceiptRow, (intent.job_id, intent.generation, intent.sequence)
        )
        if (
            authority.expires_at != grant.expires_at
            or (authority.job_id, authority.generation, authority.boundary)
            != (grant.job_id, grant.generation, grant.boundary)
            or op.generation != grant.generation
            or (intent.job_id, intent.generation) != (op.job_id, op.generation)
            or intent.phase is not expected_phase
            or intent.outcome != "intent"
            or retained is None
            or retained.receipt_sha256 != intent.sha256()
            or UpgradeReceipt.model_validate(retained.payload) != intent
            or authority.consumed_at >= authority.expires_at
            or authority.consumed_at > now
            or intent.occurred_at > authority.consumed_at
        ):
            raise UpgradeConflict("Consumed activation replay differs from its retained intent")
        return  # A durable acknowledgment, not another activation or renewed grant.
    if (
        authority is None
        or authority.consumed_at is not None
        or authority.expires_at <= now
        or authority.expires_at != grant.expires_at
        or (authority.job_id, authority.generation, authority.boundary)
        != (grant.job_id, grant.generation, grant.boundary)
        or op.generation != grant.generation
        or op.finished_at is not None
        or op.cancel_requested
        or (intent.job_id, intent.generation) != (op.job_id, op.generation)
        or intent.phase is not expected_phase
        or intent.outcome != "intent"
        or not 0 <= (now - intent.occurred_at).total_seconds() <= 30
    ):
        raise UpgradeConflict("Activation grant is stale, expired or mismatched")
    if row.execution_plan is None:
        raise UpgradeConflict("Accepted execution plan is unavailable")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    await verify_fenced(session, plan)
    await verify_activation_ready(session, op, grant.boundary, plan)
    await verify_current_schema(session, op, plan)
    await verify_backup_boundary(plan, grant.boundary, settings or get_settings())
    await verify_live_core_drain(session, plan, settings or get_settings(), admissions)
    await authorize(session, initiating_actor(row))
    now = datetime.now(UTC)
    if now >= authority.expires_at or not 0 <= (now - intent.occurred_at).total_seconds() <= 30:
        raise UpgradeConflict("Activation authority expired during prerequisite verification")
    await append_receipt(session, intent, executor)
    authority.consumed_at = now
    current = UpgradeOperation.model_validate(row.payload)
    row.phase = expected_phase.value
    row.payload = current.model_copy(
        update={"phase": expected_phase, "updated_at": now}
    ).model_dump(mode="json")
    await session.flush()


async def verify_backup_boundary(
    plan: UpgradeExecutionPlan, boundary: str, settings: Settings
) -> None:
    if boundary != "migration":
        return
    require_migration_backup(plan)
    if (
        plan.backup is None
        or plan.initial_schema_head is None
        or plan.initial_schema_sha256 is None
    ):
        raise UpgradeConflict("Migration has no accepted deployed-schema backup authority")
    await asyncio.to_thread(
        verify_prepared_backup,
        Path(settings.upgrade_prepared_dir) / "backups",
        plan.backup,
        project=settings.upgrade_project,
        schema_head=plan.initial_schema_head,
        schema_sha256=plan.initial_schema_sha256,
    )


async def verify_live_core_drain(
    session: AsyncSession,
    plan: UpgradeExecutionPlan,
    settings: Settings,
    admissions: "CoreAdmissions | None",
) -> None:
    """An executor's staging assertion cannot substitute for current owner observations."""
    if plan.operation.kind.value != "control_plane":
        return
    from coire_api.nodes_client import NodeClient, NodeError
    from coire_api.upgrades.inventory import core_inventory, core_required_owners
    from coire_core.errors import UpgradeUnavailable
    from coire_core.models.upgrades import NODE_UPGRADE_OWNERS

    if admissions is None:
        raise UpgradeUnavailable("Core activation has no live request inventory owner")
    snapshot = await core_inventory(session, settings, plan, admissions)
    if (snapshot.job_id, snapshot.generation, snapshot.project, snapshot.release_sha256) != (
        plan.operation.job_id,
        plan.operation.generation,
        settings.upgrade_project,
        plan.operation.source_sha256,
    ) or not snapshot.idle(core_required_owners(plan)):
        raise UpgradeConflict("Core activation still owns active, cleaning or unknown work")
    observations = [snapshot.observed_at]
    async with NodeClient(settings, timeout=5) as client:
        for node in ("coire-edge-a", "coire-edge-b"):
            if node not in plan.maintenance.targets:
                continue
            try:
                status = await client.upgrade_status(node)
                inventory = await client.upgrade_inventory(
                    node, plan.operation.job_id, plan.operation.generation
                )
            except NodeError:
                raise UpgradeUnavailable(
                    "Core activation Studio ownership is unavailable"
                ) from None
            if (
                not status.bootstrap_ready
                or status.runtime_sha256 is None
                or (
                    inventory.node,
                    inventory.job_id,
                    inventory.generation,
                    inventory.runtime_sha256,
                )
                != (node, plan.operation.job_id, plan.operation.generation, status.runtime_sha256)
                or not inventory.idle(NODE_UPGRADE_OWNERS)
            ):
                raise UpgradeConflict("Core activation Studio identity or drain ownership differs")
            observations.extend((status.observed_at, inventory.observed_at))
    now = datetime.now(UTC)
    if any(not 0 <= (now - observed).total_seconds() <= 5 for observed in observations):
        raise UpgradeConflict("Core activation drain observations are stale or in the future")
