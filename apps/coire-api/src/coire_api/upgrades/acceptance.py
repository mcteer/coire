"""Accept immutable Studio intent only against current independent recovery ownership."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import TrainingJobRow, UpgradeExecutorRow, UpgradeReleaseRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import accept, get_operation, get_release, operation_lock
from coire_api.upgrades.service import prepare_plan
from coire_api.upgrades.targets import target_statuses
from coire_core.errors import UpgradeConflict, UpgradeNotFound
from coire_core.models.upgrades import (
    StudioName,
    UpgradeAction,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReleaseRegister,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
    UpgradeTiming,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release


async def capture_drain_budget(session: AsyncSession, configured_s: int) -> int:
    """Leave pending trainers their original checkpoint completion budget.

    Maintenance blocks new replication/extraction/validation admissions. A
    trainer waits only 60 seconds for checkpoint commit, so a busy upgrade must
    close unchanged well before that bound instead of failing existing work.
    Take the same lock as new Core work; capture once, never renew an operation.
    """
    await operation_lock(session)
    pending = await session.scalar(
        select(TrainingJobRow.id)
        .where(TrainingJobRow.state.not_in(("paused", "succeeded", "failed", "cancelled")))
        .limit(1)
    )
    return min(configured_s, 15) if pending is not None else configured_s


async def accept_studio(
    session: AsyncSession,
    principal: Principal,
    node: StudioName,
    request: UpgradeStartRequest,
    settings: Settings,
    *,
    action: UpgradeAction = UpgradeAction.UPGRADE,
    parent_job_id: str | None = None,
) -> tuple[UpgradeOperation, bool]:
    from coire_api.upgrades.host_hold import reconcile_orphan_hold

    await reconcile_orphan_hold(session, principal, settings)
    source_row = await session.scalar(
        select(UpgradeReleaseRow).where(
            UpgradeReleaseRow.manifest_sha256 == request.expected_source_sha256
        )
    )
    if source_row is None:
        raise UpgradeNotFound("Expected Source release is not registered")
    operation, created = await accept(
        session,
        principal,
        request,
        source_row.id,
        node=node,
        action=action,
        parent_job_id=parent_job_id,
        timing=UpgradeTiming(
            drain_s=await capture_drain_budget(session, settings.upgrade_drain_s),
            readiness_s=settings.upgrade_readiness_s,
            recovery_s=settings.upgrade_recovery_s,
            gateway_outage_s=settings.upgrade_gateway_outage_s,
        ),
    )
    if not created:
        return operation, False
    statuses = await target_statuses(session, settings)
    selected = next(item for item in statuses.items if item.node == node)
    if selected.readiness != "ready" or selected.observed_sha256 != request.expected_source_sha256:
        raise UpgradeConflict("Studio Source or independent recovery readiness differs")
    source, target = (
        await get_release(session, operation.source_release_id),
        await get_release(session, operation.target_release_id),
    )
    for release in (source, target):
        prepared = await asyncio.to_thread(
            verify_prepared_release,
            Path(settings.upgrade_prepared_dir),
            UpgradeReleaseRegister(
                prepared_artifact_id=release.prepared_artifact_id,
                expected_sha256=release.manifest_sha256,
            ),
        )
        if prepared != release:
            raise UpgradeConflict("Registered Studio artifact provenance changed")
    peer_sha = None
    if source.manifest.paired_required or target.manifest.paired_required:
        peer = next(item for item in statuses.items if item.node is not None and item.node != node)
        if peer.readiness != "ready" or peer.release_id is None:
            raise UpgradeConflict("Paired upgrade peer independent recovery is not ready")
        peer_release = await get_release(session, peer.release_id)
        peer_sha = peer_release.manifest.environment_sha256
    plan = await prepare_plan(session, operation, settings, peer_runtime_sha256=peer_sha)
    from coire_api.upgrades.host_hold import operation_hold

    await asyncio.to_thread(operation_hold, settings, plan)
    return operation, True


async def accept_rollback(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    request: UpgradeRollbackRequest,
    settings: Settings,
) -> tuple[UpgradeOperation, bool]:
    """Reverse only a successful retained operation through the same fresh acceptance checks."""
    await operation_lock(session)
    await authorize(session, principal)
    parent = UpgradeOperation.model_validate((await get_operation(session, job_id)).payload)
    if parent.phase is not UpgradePhase.SUCCEEDED:
        raise UpgradeConflict("Rollback requires a successful retained operation")
    reversed_request = UpgradeStartRequest(
        release_id=parent.source_release_id,
        expected_source_sha256=request.expected_source_sha256,
        idempotency_key=request.idempotency_key,
        acknowledge_maintenance=request.acknowledge_maintenance,
    )
    if parent.kind is UpgradeKind.CONTROL_PLANE:
        return await accept_core(
            session,
            principal,
            reversed_request,
            settings,
            action=UpgradeAction.ROLLBACK,
            parent_job_id=parent.job_id,
        )
    if parent.node is None:
        raise UpgradeConflict("Retained Studio rollback has no node authority")
    return await accept_studio(
        session,
        principal,
        parent.node,
        reversed_request,
        settings,
        action=UpgradeAction.ROLLBACK,
        parent_job_id=parent.job_id,
    )


async def accept_core(
    session: AsyncSession,
    principal: Principal,
    request: UpgradeStartRequest,
    settings: Settings,
    *,
    action: UpgradeAction = UpgradeAction.UPGRADE,
    parent_job_id: str | None = None,
) -> tuple[UpgradeOperation, bool]:
    """Freeze Core authority only against a freshly verified independent recovery owner."""
    from coire_api.upgrades.host_hold import operation_hold, reconcile_orphan_hold

    await reconcile_orphan_hold(session, principal, settings)
    source_row = await session.scalar(
        select(UpgradeReleaseRow).where(
            UpgradeReleaseRow.manifest_sha256 == request.expected_source_sha256
        )
    )
    if source_row is None:
        raise UpgradeNotFound("Expected Core Source release is not registered")
    operation, created = await accept(
        session,
        principal,
        request,
        source_row.id,
        action=action,
        parent_job_id=parent_job_id,
        timing=UpgradeTiming(
            drain_s=await capture_drain_budget(session, settings.upgrade_drain_s),
            readiness_s=settings.upgrade_readiness_s,
            recovery_s=settings.upgrade_recovery_s,
            gateway_outage_s=settings.upgrade_gateway_outage_s,
        ),
    )
    if operation.kind is not UpgradeKind.CONTROL_PLANE:
        raise UpgradeConflict("Core start requires control-plane releases")
    if not created:
        return operation, False
    owner = await session.get(UpgradeExecutorRow, settings.upgrade_project, with_for_update=True)
    if (
        owner is None
        or not owner.bootstrap_ready
        or owner.identity != settings.upgrade_executor_id
        or owner.source_release_id != operation.source_release_id
        or not 0 <= (datetime.now(UTC) - owner.heartbeat_at).total_seconds() <= 10
    ):
        raise UpgradeConflict("Independent Core recovery owner is not freshly verified")
    statuses = await target_statuses(session, settings)
    selected = next(item for item in statuses.items if item.kind is UpgradeKind.CONTROL_PLANE)
    if (
        selected.readiness != "ready"
        or selected.release_id != operation.source_release_id
        or selected.observed_sha256 != request.expected_source_sha256
    ):
        raise UpgradeConflict("Core Source or independent recovery readiness differs")
    for release_id in (operation.source_release_id, operation.target_release_id):
        release = await get_release(session, release_id)
        if release.manifest.host_release_key is None:
            raise UpgradeConflict("Core release has no verified source-independent host binding")
        prepared = await asyncio.to_thread(
            verify_prepared_release,
            Path(settings.upgrade_prepared_dir),
            UpgradeReleaseRegister(
                prepared_artifact_id=release.prepared_artifact_id,
                expected_sha256=release.manifest_sha256,
            ),
        )
        if prepared != release:
            raise UpgradeConflict("Registered Core artifact provenance changed")
    plan = await prepare_plan(session, operation, settings)
    await asyncio.to_thread(operation_hold, settings, plan)
    return operation, True
