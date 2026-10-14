"""Exclude host effects across the executor handover's kernel ownership gap."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeHandoverRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import operation_lock
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreHandoverCreation,
    CoreHandoverPreparation,
    DeploymentHandoverHold,
    DeploymentOperationHold,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def publish_handover_hold(settings: Settings, preparation: CoreHandoverPreparation) -> None:
    root = Path(settings.upgrade_coordination_dir)
    if not settings.upgrades_enabled or not root.exists():
        raise UpgradeUnavailable("Executor handover incumbent coordination is absent")
    store = PrivateUpgradeStore(root)
    with store.ownership("deployment-admission.lock"):
        owner = idle_executor_owner(store, settings.upgrade_project, settings.upgrade_executor_id)
        incumbent = preparation.incumbent
        if owner is None or (
            owner.project,
            owner.executor_runtime_id,
            owner.executor_container_id,
            owner.source_sha256,
            owner.generation + 1,
        ) != (
            incumbent.source.manifest.project,
            incumbent.executor_runtime_id,
            incumbent.executor_container_id,
            incumbent.source.manifest_sha256,
            preparation.generation,
        ):
            raise UpgradeConflict("Executor handover hold has no exact retained incumbent")
        app_hold = store.read("operation-hold.json", DeploymentOperationHold)
        if app_hold is not None and (app_hold.project != owner.project or not app_hold.released):
            raise UpgradeConflict("Application maintenance retains deployment ownership")
        held = store.read("handover-hold.json", DeploymentHandoverHold)
        expected = DeploymentHandoverHold(
            project=owner.project,
            preparation_sha256=preparation.sha256(),
            generation=preparation.generation,
            incumbent_runtime_id=incumbent.executor_runtime_id,
            incumbent_container_id=incumbent.executor_container_id,
            candidate_runtime_id=preparation.candidate_runtime_id,
            updated_at=datetime.now(UTC),
        )
        if held is not None:
            if held.model_dump(exclude={"updated_at", "released"}) == expected.model_dump(
                exclude={"updated_at", "released"}
            ):
                if held.released:
                    raise UpgradeConflict("Closed executor handover cannot reopen host exclusion")
                return
            if (
                not held.released
                or held.project != owner.project
                or held.generation >= expected.generation
            ):
                raise UpgradeConflict("Prior executor handover retains deployment ownership")
        store.write("handover-hold.json", expected)
        if (
            idle_executor_owner(store, settings.upgrade_project, settings.upgrade_executor_id)
            != owner
        ):
            raise UpgradeConflict("Executor incumbent changed during handover publication")


async def reconcile_orphan_handover_hold(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
) -> None:
    """A rolled-back acceptance may close only before any retained creation intent."""
    await operation_lock(session)
    await authorize(session, principal)

    def read_current() -> tuple[PrivateUpgradeStore, DeploymentHandoverHold] | None:
        root = Path(settings.upgrade_coordination_dir)
        if not root.exists():
            return None
        store = PrivateUpgradeStore(root)
        held = store.read("handover-hold.json", DeploymentHandoverHold)
        return (store, held) if held is not None and not held.released else None

    current = await asyncio.to_thread(read_current)
    if current is None:
        return
    store, held = current
    if held.project != settings.upgrade_project:
        raise UpgradeConflict("Orphan executor handover belongs to another project")
    retained = await session.scalar(
        select(UpgradeHandoverRow.id)
        .where(
            (UpgradeHandoverRow.preparation_sha256 == held.preparation_sha256)
            | (UpgradeHandoverRow.active_slot == 1)
        )
        .limit(1)
    )
    if retained is not None:
        raise UpgradeConflict("Executor handover retains database authority")

    def release() -> None:
        with store.ownership("deployment-admission.lock"):
            owner = idle_executor_owner(
                store, settings.upgrade_project, settings.upgrade_executor_id
            )
            if owner is None or (
                owner.executor_runtime_id,
                owner.executor_container_id,
                owner.generation + 1,
            ) != (held.incumbent_runtime_id, held.incumbent_container_id, held.generation):
                raise UpgradeConflict("Orphan handover has no retained incumbent writer")
            journal_root = (
                Path(settings.upgrade_state_dir) / "bootstrap/handover" / str(held.generation)
            )
            if journal_root.exists():
                journal = PrivateUpgradeStore(journal_root)
                if journal.read("creation.json", CoreHandoverCreation) is not None:
                    raise UpgradeConflict(
                        "Executor creation intent requires lifecycle reconciliation"
                    )
            if store.read("handover-hold.json", DeploymentHandoverHold) != held:
                raise UpgradeConflict("Orphan executor handover hold changed")
            store.write(
                "handover-hold.json",
                held.model_copy(update={"released": True, "updated_at": datetime.now(UTC)}),
            )
            if (
                idle_executor_owner(store, settings.upgrade_project, settings.upgrade_executor_id)
                != owner
            ):
                raise UpgradeConflict("Orphan handover incumbent changed during publication")

    await asyncio.to_thread(release)
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.orphan_hold_released",
        target_type="upgrade_handover",
        target_id=held.preparation_sha256,
        context={"generation": held.generation},
    )
