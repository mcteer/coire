"""Public upgrade readiness comes from current authenticated recovery observations."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeMaintenanceRow, UpgradeReleaseRow
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.upgrades.core_targets import core_target_status
from coire_api.upgrades.host_hold import deployment_readiness
from coire_api.upgrades.persistence import get_operation, get_release
from coire_core.errors import UpgradeNotFound
from coire_core.models.upgrades import (
    NodeUpgradeStatus,
    StudioName,
    UpgradeAction,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRelease,
    UpgradeTargetList,
    UpgradeTargetStatus,
    release_pair_compatible,
)
from coire_core.settings import Settings


async def target_statuses(session: AsyncSession, settings: Settings) -> UpgradeTargetList:
    now = datetime.now(UTC)
    fences = tuple(
        UpgradeMaintenance.model_validate(row.payload)
        for row in (
            await session.scalars(
                select(UpgradeMaintenanceRow)
                .where(UpgradeMaintenanceRow.released.is_(False))
                .limit(16)
            )
        ).all()
    )
    async with NodeClient(settings, timeout=5) as client:

        async def observe(node: StudioName) -> NodeUpgradeStatus | None:
            try:
                return await client.upgrade_status(node)
            except NodeError:
                return None

        observations = await asyncio.gather(observe("coire-edge-a"), observe("coire-edge-b"))
    items: list[UpgradeTargetStatus] = []
    host_state = await asyncio.to_thread(deployment_readiness, settings)
    nodes: tuple[StudioName, StudioName] = ("coire-edge-a", "coire-edge-b")
    for node, observed in zip(nodes, observations, strict=True):
        reasons: list[str] = []
        if host_state != "ready":
            reasons.append(
                "cluster_operation_active"
                if host_state == "maintenance"
                else "shared_deployment_ownership_unavailable"
            )
        release = None
        if (
            observed is None
            or not 0 <= (datetime.now(UTC) - observed.observed_at).total_seconds() <= 5
        ):
            reasons.append("node_recovery_observation_unavailable")
        elif not observed.bootstrap_ready:
            reasons.append("independent_recovery_bootstrap_unready")
        if observed is not None and observed.release_sha256 is not None:
            row = await session.scalar(
                select(UpgradeReleaseRow).where(
                    UpgradeReleaseRow.manifest_sha256 == observed.release_sha256
                )
            )
            if row is not None:
                release = UpgradeRelease.model_validate(row.payload)
            if release is None:
                reasons.append("selected_release_unregistered")
            elif release.manifest.kind is not UpgradeKind.STUDIO or (
                release.manifest.environment_sha256 != observed.runtime_sha256
            ):
                reasons.append("selected_runtime_identity_differs")
        else:
            reasons.append("selected_release_unknown")
        retained_sha = None
        rollback_job = None
        rollback_reasons: list[str] = []
        if observed is not None and observed.reason == "retained_source_unavailable":
            reasons.append("retained_source_unavailable")
        if release is not None and observed is not None and observed.retained_job_id is not None:
            try:
                parent = UpgradeOperation.model_validate(
                    (await get_operation(session, observed.retained_job_id)).payload
                )
                retained = await get_release(session, parent.source_release_id)
            except UpgradeNotFound:
                reasons.append("retained_operation_unregistered")
            else:
                if (
                    parent.phase is UpgradePhase.SUCCEEDED
                    and parent.node == node
                    and parent.target_release_id == release.release_id
                    and parent.target_sha256 == observed.release_sha256
                    and parent.source_sha256
                    == observed.retained_source_sha256
                    == retained.manifest_sha256
                ):
                    retained_sha = retained.manifest_sha256
                    if release_pair_compatible(
                        release, retained, UpgradeAction.ROLLBACK, parent.job_id
                    ):
                        rollback_job = parent.job_id
                    else:
                        rollback_reasons.append("retained_source_incompatible")
                elif parent.phase is not UpgradePhase.SUCCEEDED:
                    rollback_reasons.append("retained_operation_not_successful")
                else:
                    reasons.append("retained_source_identity_differs")
        fence = next(
            (item for item in fences if node in item.targets or "cluster" in item.targets), None
        )
        if fence is not None:
            reasons.append("maintenance_active")
        elif observed is not None and observed.job_id is not None:
            reasons.append("native_operation_retains_ownership")
        items.append(
            UpgradeTargetStatus(
                kind=UpgradeKind.STUDIO,
                node=node,
                desired_sha256=release.manifest_sha256 if release else None,
                observed_sha256=observed.release_sha256 if observed else None,
                release_id=release.release_id if release else None,
                retained_sha256=retained_sha,
                rollback_job_id=rollback_job,
                rollback_blocking_reasons=tuple(rollback_reasons),
                rollback_eligible=rollback_job is not None and not reasons and fence is None,
                readiness="maintenance" if fence else ("not_ready" if reasons else "ready"),
                blocking_reasons=tuple(reasons),
                observed_at=observed.observed_at if observed else now,
                bootstrap_ready=observed.bootstrap_ready if observed else False,
            )
        )
    items.append(await core_target_status(session, settings, host_state))
    return UpgradeTargetList(items=tuple(items))
