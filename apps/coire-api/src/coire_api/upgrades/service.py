"""Fixed accepted authority and operator intent; effects belong to recovery owners."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import ModelRow, ModelVariantRow, NodeRow, UpgradeReceiptRow, VariantCopyRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.grants import initiating_actor
from coire_api.upgrades.persistence import (
    append_receipt,
    get_operation,
    get_release,
    operation_lock,
)
from coire_api.upgrades.schema_identity import inspect_schema
from coire_api.upgrades.state import validate_progress
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.acquisition import VariantState
from coire_core.models.registry import ModelState
from coire_core.models.sharding import LinkObservation, ProbeOutcome, ProbeTransport
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    BackupEvidence,
    NodeUpgradeCancel,
    UpgradeAction,
    UpgradeCancelRequest,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeMigrationNotStarted,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_backups import select_prepared_backup, verify_prepared_backup
from coire_scheduler.maintenance import dependency_scope


async def smoke_asset(
    session: AsyncSession, model_id: UUID | None, nodes: tuple[str, ...]
) -> tuple[UUID, UUID, str, str]:
    if model_id is None or not nodes:
        raise UpgradeUnavailable("A verified tiny smoke asset must be prepared first")
    model = await session.get(ModelRow, model_id)
    if (
        model is None
        or model.state is not ModelState.READY
        or model.source != "studio"
        or model.kind != "language_model"
        or model.backend != "mlx_lm"
    ):
        raise UpgradeUnavailable("Configured smoke model is not an eligible local registry asset")
    variants = (
        await session.scalars(
            select(ModelVariantRow).where(
                ModelVariantRow.model_id == model_id,
                ModelVariantRow.is_default.is_(True),
                ModelVariantRow.state == VariantState.READY,
                ModelVariantRow.validated.is_(True),
                ModelVariantRow.harness_verified.is_(True),
                ModelVariantRow.backend == "mlx_lm",
            )
        )
    ).all()
    if len(variants) != 1:
        raise UpgradeUnavailable(
            "Configured smoke model requires exactly one verified default variant"
        )
    variant = variants[0]
    if not 0 < variant.byte_size <= 1024**3:
        raise UpgradeUnavailable("Configured smoke variant must be verified and at most one GiB")
    copies = (
        await session.execute(
            select(VariantCopyRow, NodeRow.name)
            .join(NodeRow, NodeRow.id == VariantCopyRow.node_id)
            .where(VariantCopyRow.variant_id == variant.id, NodeRow.name.in_(nodes))
        )
    ).all()
    if (
        {name for _, name in copies} != set(nodes)
        or len(copies) != len(set(nodes))
        or any(
            not copy.verified or not 0 < copy.bytes <= 1024**3 or copy.manifest_sha256 is None
            for copy, _ in copies
        )
        or len({copy.manifest_sha256 for copy, _ in copies}) != 1
    ):
        raise UpgradeUnavailable("Smoke asset copies require matching verified manifests")
    digest = copies[0][0].manifest_sha256
    assert digest is not None
    return model.id, variant.id, variant.slug, digest


async def prepare_plan(
    session: AsyncSession,
    operation: UpgradeOperation,
    settings: Settings,
    *,
    peer_runtime_sha256: str | None = None,
    backup: BackupEvidence | None = None,
) -> UpgradeExecutionPlan:
    await operation_lock(session)
    row = await get_operation(session, operation.job_id, lock=True)
    current = UpgradeOperation.model_validate(row.payload)
    if (
        current.authority_sha256() != operation.authority_sha256()
        or current.finished_at is not None
    ):
        raise UpgradeConflict("Accepted operation changed before preparation")
    if row.execution_plan is not None:
        return UpgradeExecutionPlan.model_validate(row.execution_plan)
    await authorize(session, initiating_actor(row))
    source = await get_release(session, current.source_release_id)
    target = await get_release(session, current.target_release_id)
    initial_head = initial_digest = schema_after = None
    if current.kind is UpgradeKind.CONTROL_PLANE:
        initial_head, initial_digest = await inspect_schema(session)
        schema_after = (
            initial_head
            if current.action is UpgradeAction.ROLLBACK
            or source.manifest.schema_head == target.manifest.schema_head
            else target.manifest.schema_head
        )
        if initial_head != schema_after:
            backup_root = Path(settings.upgrade_prepared_dir) / "backups"
            if backup is None:
                backup = await asyncio.to_thread(
                    select_prepared_backup,
                    backup_root,
                    project=settings.upgrade_project,
                    schema_head=initial_head,
                    schema_sha256=initial_digest,
                )
            else:
                await asyncio.to_thread(
                    verify_prepared_backup,
                    backup_root,
                    backup,
                    project=settings.upgrade_project,
                    schema_head=initial_head,
                    schema_sha256=initial_digest,
                )
    scope = dependency_scope(
        current.kind,
        node=current.node,
        cohort=current.cohort,
        schema_changed=initial_head != schema_after,
        paired=source.manifest.paired_required or target.manifest.paired_required,
    )
    studio_scope = tuple(item for item in scope if item in {"coire-edge-a", "coire-edge-b"})
    asset_nodes = studio_scope if current.kind is UpgradeKind.STUDIO else ("coire-edge-a",)
    model_id, variant_id, slug, digest = await smoke_asset(
        session, settings.upgrade_smoke_model_id, asset_nodes
    )
    now = datetime.now(UTC)
    value = UpgradeExecutionPlan(
        operation=current,
        source=source,
        target=target,
        maintenance=UpgradeMaintenance(
            job_id=current.job_id,
            generation=current.generation,
            targets=scope,
            initiator_user_id=current.user_id,
            state="fencing",
            updated_at=now,
            drain_deadline_at=now + timedelta(seconds=current.timing.drain_s),
        ),
        authorization_expires_at=now + timedelta(seconds=30),
        model_id=model_id,
        model_variant_id=variant_id,
        model_slug=slug,
        model_manifest_sha256=digest,
        peer_runtime_sha256=peer_runtime_sha256,
        backup=backup,
        initial_schema_head=initial_head,
        initial_schema_sha256=initial_digest,
        schema_head_after=schema_after,
    )
    row.execution_plan = value.model_dump(mode="json")
    await session.flush()
    return value


async def request_cancel(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    request: UpgradeCancelRequest,
    *,
    settings: Settings | None = None,
) -> UpgradeOperation:
    await operation_lock(session)
    row = await get_operation(session, job_id, lock=True)
    await authorize(session, principal)
    current = UpgradeOperation.model_validate(row.payload)
    if current.finished_at is not None or current.cancel_requested:
        return current
    if current.kind is UpgradeKind.CONTROL_PLANE and not current.cancellation_allowed:
        raise UpgradeConflict(
            "Core runtime selection is being published; cancellation boundary has passed"
        )
    if current.kind is UpgradeKind.CONTROL_PLANE and current.phase is UpgradePhase.COMMITTING:
        decision_row = await session.get(
            UpgradeReceiptRow, (current.job_id, current.generation, current.receipt_sequence)
        )
        decision = (
            UpgradeReceipt.model_validate(decision_row.payload)
            if decision_row is not None
            else None
        )
        if (
            decision is not None
            and decision.phase is UpgradePhase.COMMITTING
            and decision.outcome == "complete"
        ):
            raise UpgradeConflict(
                "Core runtime selection is being published; cancellation boundary has passed"
            )
    if current.kind is UpgradeKind.STUDIO:
        from coire_api.nodes_client import NodeClient, NodeError
        from coire_core.settings import get_settings

        if current.node is None or row.execution_plan is None:
            raise UpgradeUnavailable("Studio cancellation has no accepted node authority")
        plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
        try:
            async with NodeClient(settings or get_settings(), timeout=5) as client:
                status = await client.cancel_upgrade(
                    current.node,
                    job_id,
                    NodeUpgradeCancel(
                        generation=current.generation,
                        unstarted_plan=plan
                        if (
                            current.phase is UpgradePhase.ACCEPTED and current.receipt_sequence == 0
                        )
                        else None,
                    ),
                    after_sequence=current.receipt_sequence,
                )
        except NodeError:
            raise UpgradeUnavailable(
                "Studio cancellation acknowledgement remains unverified"
            ) from None
        for receipt in status.receipts:
            await ingest_receipt(session, receipt, principal)
        current = UpgradeOperation.model_validate(row.payload)
        if status.phase in TERMINAL_UPGRADE_PHASES:
            if current.finished_at is None:
                raise UpgradeUnavailable(
                    "Studio terminal cancellation progress is still reconciling"
                )
            await write_principal_audit(
                session,
                principal=principal,
                action="upgrades.cancel.request",
                target_type="upgrade",
                target_id=job_id,
                detail={
                    "reason": request.reason,
                    "generation": current.generation,
                    "terminal_phase": current.phase.value,
                },
            )
            return current
    value = current.model_copy(
        update={
            "cancel_requested": True,
            "reason": "administrator_cancelled",
            "updated_at": datetime.now(UTC),
        }
    )
    row.payload = value.model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.cancel.request",
        target_type="upgrade",
        target_id=job_id,
        detail={"reason": request.reason, "generation": current.generation},
    )
    await session.flush()
    return value


async def ingest_receipt(
    session: AsyncSession,
    receipt: UpgradeReceipt,
    principal: Principal,
    *,
    settings: Settings | None = None,
) -> bool:
    """Atomically reconcile progress against accepted authority and durable history."""
    if receipt.effect_id.startswith("expired-recovery-"):
        raise UpgradeConflict(
            "After-deadline resolution requires distinct linked recovery authority"
        )
    await operation_lock(session)
    row = await get_operation(session, receipt.job_id, lock=True)
    operation = UpgradeOperation.model_validate(row.payload)
    existing = await session.get(
        UpgradeReceiptRow, (receipt.job_id, receipt.generation, receipt.sequence)
    )
    if existing is not None:
        # Replay stays legal after terminal reconciliation, without reopening ownership.
        return await append_receipt(session, receipt, principal)
    if row.execution_plan is None:
        raise UpgradeConflict("Progress requires an accepted immutable execution plan")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    rows = (
        await session.scalars(
            select(UpgradeReceiptRow)
            .where(
                UpgradeReceiptRow.job_id == operation.job_id,
                UpgradeReceiptRow.generation == operation.generation,
            )
            .order_by(UpgradeReceiptRow.sequence)
            .limit(4097)
        )
    ).all()
    history = tuple(UpgradeReceipt.model_validate(item.payload) for item in rows)
    if len(history) != operation.receipt_sequence or len(history) >= 4096:
        raise UpgradeUnavailable("Progress history is incomplete or exceeds its bound")
    for sequence, (stored, value) in enumerate(zip(rows, history, strict=True), 1):
        if value.sequence != sequence or value.sha256() != stored.receipt_sha256:
            raise UpgradeUnavailable("Progress history failed content verification")
    if sum(len(item.evidence.encode()) for item in (*history, receipt)) > 256 * 1024:
        raise UpgradeConflict("Operation diagnostic evidence exceeds its retained bound")
    not_started = (
        UpgradeMigrationNotStarted.model_validate(row.migration_execution)
        if row.migration_execution is not None
        and row.migration_execution.get("state") == "not_started"
        else None
    )
    validate_progress(operation, receipt, plan, history, migration_not_started=not_started)
    if (
        operation.kind is UpgradeKind.CONTROL_PLANE
        and receipt.phase is UpgradePhase.COMMITTING
        and receipt.outcome == "complete"
    ):
        await authorize(session, initiating_actor(row))
    if operation.kind is UpgradeKind.CONTROL_PLANE and receipt.phase is UpgradePhase.SUCCEEDED:
        from coire_api.upgrades.host_hold import require_published_target
        from coire_core.settings import get_settings

        await asyncio.to_thread(require_published_target, settings or get_settings(), plan)
    appended = await append_receipt(session, receipt, principal)
    if not appended:
        return False
    if (
        operation.kind is UpgradeKind.STUDIO
        and receipt.phase is UpgradePhase.VERIFYING_SOURCE
        and receipt.outcome == "complete"
        and (plan.source.manifest.paired_required or plan.target.manifest.paired_required)
    ):
        evidence = UpgradeEvidence.model_validate_json(receipt.evidence)
        if evidence.collective_verified is not True:
            from coire_api.sharding import append_observation

            source_hash = plan.source.manifest.environment_sha256
            pair = (
                (source_hash, plan.peer_runtime_sha256)
                if operation.node == "coire-edge-a"
                else (plan.peer_runtime_sha256, source_hash)
            )
            await append_observation(
                session,
                LinkObservation(
                    id=uuid5(NAMESPACE_URL, "coire-upgrade-source-ineligible/" + receipt.sha256()),
                    node_a="coire-edge-a",
                    node_b="coire-edge-b",
                    transport=ProbeTransport.JACCL,
                    outcome=ProbeOutcome.FAILED,
                    os_version_a="unknown",
                    os_version_b="unknown",
                    engine_version="unknown",
                    runtime_sha256_a=pair[0],
                    runtime_sha256_b=pair[1],
                    reason="restored source collective qualification unavailable",
                    observed_at=receipt.occurred_at,
                ),
            )
    current = UpgradeOperation.model_validate(row.payload)
    now = datetime.now(UTC)
    terminal = receipt.phase in TERMINAL_UPGRADE_PHASES
    updates: dict[str, object] = {"phase": receipt.phase, "updated_at": now}
    if operation.kind is UpgradeKind.CONTROL_PLANE:
        if receipt.phase is UpgradePhase.COMMITTING and receipt.outcome == "complete":
            updates["cancellation_allowed"] = False
        elif receipt.phase is UpgradePhase.RESTORING:
            updates["cancellation_allowed"] = True
    if terminal:
        updates["finished_at"] = now
        updates["cancellation_allowed"] = False
        if operation.kind is UpgradeKind.CONTROL_PLANE:
            evidence = UpgradeEvidence.model_validate_json(receipt.evidence)
            updates["retained_schema_head"] = evidence.schema_head
        row.active_slot = None
    row.phase = receipt.phase.value
    row.payload = current.model_copy(update=updates).model_dump(mode="json")
    # Fences remain persisted until the coordinator verifies release on each owner.
    await session.flush()
    return True
