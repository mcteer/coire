"""Durable cluster intent excludes host deployment and credential rotation."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeOperationRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import operation_lock
from coire_core.deploy import deployment_mutex, idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    DeploymentHandoverHold,
    DeploymentOperationHold,
    DeploymentOwnership,
    DeploymentSelectionReceipt,
    UpgradeExecutionPlan,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def require_published_target(settings: Settings, plan: UpgradeExecutionPlan) -> None:
    """Success ingestion requires the independent owner's actual published selection."""
    root = Path(settings.upgrade_coordination_dir)
    if not root.exists():
        raise UpgradeUnavailable("Core runtime publication is unavailable")
    store = PrivateUpgradeStore(root)
    owner = store.read("ownership.json", DeploymentOwnership)
    receipt = store.read("selection.json", DeploymentSelectionReceipt)
    runtime = store.read("runtime-selection.json", CoreRuntimeSelection)
    target = plan.target
    if (
        owner is None
        or receipt is None
        or runtime is None
        or owner.state != "committed"
        or owner.stage != "selected"
        or owner.owner_kind != "executor"
        or owner.owner_identity != settings.upgrade_executor_id
        or owner.project != settings.upgrade_project
        or (owner.job_id, owner.operation_generation, owner.source_sha256)
        != (plan.operation.job_id, plan.operation.generation, plan.source.manifest_sha256)
        or owner.committed_sha256 != target.manifest.compose_sha256
        or (receipt.project, receipt.owner_identity, receipt.boot_identity, receipt.generation)
        != (owner.project, owner.owner_identity, owner.boot_identity, owner.generation)
        or (
            receipt.managed_release_id,
            receipt.manifest_sha256,
            receipt.release_key,
            receipt.compose_sha256,
        )
        != (
            target.release_id,
            target.manifest_sha256,
            target.manifest.host_release_key,
            target.manifest.compose_sha256,
        )
        or (runtime.project, runtime.owner_identity, runtime.generation)
        != (owner.project, owner.owner_identity, owner.generation)
        or (
            runtime.release_id,
            runtime.manifest_sha256,
            runtime.host_release_key,
            runtime.compose_sha256,
        )
        != (
            target.release_id,
            target.manifest_sha256,
            target.manifest.host_release_key,
            target.manifest.compose_sha256,
        )
        or {item.container.service: item.container.image_id for item in runtime.services}
        != {role.service: role.image_id for role in target.manifest.services}
        or any(
            (item.origin_job_id, item.origin_generation, item.origin_plan_sha256)
            != (plan.operation.job_id, plan.operation.generation, plan.authority_sha256())
            for item in runtime.services
            if item.container.service in plan.operation.cohort
        )
        or (
            runtime.migration is not None
            and receipt.protected_compose_sha256s.get(
                runtime.migration.release.manifest.host_release_key or ""
            )
            != runtime.migration.release.manifest.compose_sha256
        )
        or (
            plan.needs_migration
            and (
                runtime.migration is None
                or runtime.migration.release.release_id != target.release_id
                or runtime.migration.release.manifest_sha256 != target.manifest_sha256
                or (
                    runtime.migration.origin_job_id,
                    runtime.migration.origin_generation,
                    runtime.migration.origin_plan_sha256,
                )
                != (plan.operation.job_id, plan.operation.generation, plan.authority_sha256())
            )
        )
        or any(
            receipt.protected_compose_sha256s.get(release.manifest.host_release_key or "")
            != release.manifest.compose_sha256
            for release in (plan.source, target)
        )
    ):
        raise UpgradeConflict("Core success has no matching independent committed runtime")


@contextmanager
def admission_hold_owner(
    store: PrivateUpgradeStore, settings: Settings, identity: tuple[str, int]
) -> Iterator[DeploymentOwnership]:
    with store.ownership("deployment-admission.lock"):
        idle = idle_executor_owner(store, settings.upgrade_project, settings.upgrade_executor_id)
        if idle is not None:
            yield idle
            if (
                idle_executor_owner(store, settings.upgrade_project, settings.upgrade_executor_id)
                != idle
            ):
                raise UpgradeConflict("Idle recovery owner changed during hold publication")
            return
    with deployment_mutex(
        store, settings.upgrade_project, "control-" + identity[0], operation=identity
    ):
        owner = store.read("ownership.json", DeploymentOwnership)
        if owner is None or owner.project != settings.upgrade_project or owner.state != "committed":
            raise UpgradeConflict("Host/executor runtime ownership remains unreconciled")
        yield owner


def deployment_readiness(settings: Settings) -> Literal["ready", "maintenance", "not_ready"]:
    path = Path(settings.upgrade_coordination_dir)
    if not path.exists():
        return "not_ready"
    try:
        store = PrivateUpgradeStore(path)
        owner = store.read("ownership.json", DeploymentOwnership)
        if owner is None or owner.project != settings.upgrade_project:
            return "not_ready"
        if owner.state != "committed":
            if (
                idle_executor_owner(store, settings.upgrade_project, settings.upgrade_executor_id)
                is None
            ):
                return "not_ready"
        else:
            from coire_core.models.upgrades import DeploymentLease

            if store.read("deployment.lease", DeploymentLease) is not None:
                return "not_ready"
        handover = store.read("handover-hold.json", DeploymentHandoverHold)
        if handover is not None:
            if handover.project != settings.upgrade_project:
                return "not_ready"
            if not handover.released:
                return "maintenance"
        held = store.read("operation-hold.json", DeploymentOperationHold)
        if held is not None and held.project != settings.upgrade_project:
            return "not_ready"
        return "maintenance" if held is not None and not held.released else "ready"
    except (UpgradeConflict, UpgradeUnavailable, OSError, ValueError):
        return "not_ready"


async def reconcile_orphan_hold(
    session: AsyncSession, principal: Principal, settings: Settings
) -> None:
    """A rolled-back acceptance cannot have dispatched node work before DB commit."""
    await operation_lock(session)
    await authorize(session, principal)

    def read_current() -> tuple[PrivateUpgradeStore, DeploymentOperationHold] | None:
        path = Path(settings.upgrade_coordination_dir)
        if not path.exists():
            return None
        store = PrivateUpgradeStore(path)
        previous = store.read("operation-hold.json", DeploymentOperationHold)
        return (store, previous) if previous is not None and not previous.released else None

    current = await asyncio.to_thread(read_current)
    if current is None:
        return
    store, previous = current
    if await session.get(UpgradeOperationRow, previous.job_id) is not None:
        return

    def release() -> None:
        with admission_hold_owner(store, settings, (previous.job_id, previous.generation)):
            if store.read("operation-hold.json", DeploymentOperationHold) != previous:
                raise UpgradeConflict("Unpublished acceptance hold changed during reconciliation")
            store.write("operation-hold.json", previous.model_copy(update={"released": True}))

    await asyncio.to_thread(release)
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.host_hold.orphan_reconciled",
        target_type="upgrade",
        target_id=previous.job_id,
        detail={"generation": previous.generation, "plan_sha256": previous.plan_sha256},
    )


def operation_hold(
    settings: Settings, plan: UpgradeExecutionPlan, *, release: bool = False
) -> None:
    path = Path(settings.upgrade_coordination_dir)
    if not path.exists():
        raise UpgradeUnavailable("Shared host ownership bootstrap is unavailable")
    store = PrivateUpgradeStore(path)
    identity = (plan.operation.job_id, plan.operation.generation)
    with admission_hold_owner(store, settings, identity):
        previous = store.read("operation-hold.json", DeploymentOperationHold)
        if previous is not None and (previous.job_id, previous.generation) == identity:
            if (
                previous.plan_sha256 != plan.authority_sha256()
                or previous.source_sha256 != plan.operation.source_sha256
            ):
                raise UpgradeConflict("Cluster host hold authority differs")
            if previous.released:
                if release:
                    return
                raise UpgradeConflict("Cluster host hold has already been released")
            if release:
                store.write("operation-hold.json", previous.model_copy(update={"released": True}))
            return
        if release:
            raise UpgradeConflict("Cluster host hold release has no matching durable intent")
        if previous is not None and (not previous.released or previous.generation >= identity[1]):
            raise UpgradeConflict("A prior cluster host hold retains ownership")
        store.write(
            "operation-hold.json",
            DeploymentOperationHold(
                project=settings.upgrade_project,
                job_id=identity[0],
                generation=identity[1],
                plan_sha256=plan.authority_sha256(),
                source_sha256=plan.operation.source_sha256,
                created_at=datetime.now(UTC),
            ),
        )
