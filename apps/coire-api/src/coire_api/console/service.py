"""Pure assembly boundary for the admin console's reconciliable snapshot."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Request

from coire_api.auth import CurrentAdmin
from coire_api.deps import SessionDep, SettingsDep
from coire_api.placement.service import project_ledgers
from coire_api.routes.instances import cluster_state
from coire_core.models.console import (
    ConsoleAlert,
    ConsoleCapabilities,
    ConsoleSnapshot,
)


async def project_snapshot(
    request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    *,
    observed_at: datetime | None = None,
) -> ConsoleSnapshot:
    observed_at = observed_at or datetime.now(UTC)
    cluster = await cluster_state(principal, session, settings)
    ledgers = await project_ledgers(session)
    reconciler = getattr(request.app.state, "reconciler", None)
    statuses = dict(getattr(reconciler, "node_statuses", {}) or {})
    ledgers_by_node = {ledger.node_id: ledger for ledger in ledgers}
    for node in cluster.nodes:
        status = statuses.get(node.name)
        ledger = ledgers_by_node.get(node.id)
        if status is not None:
            node.memory_total_bytes = status.memory_total_bytes
            node.memory_free_bytes = status.memory_free_bytes
            node.disk_total_bytes = status.disk_total_bytes
            node.disk_free_bytes = status.disk_free_bytes
        node.health_reason = ledger.health_reason if ledger else None
        node.stale = (
            node.health_observed_at is None
            or (observed_at - node.health_observed_at).total_seconds()
            > settings.node_probe_interval_s * 2
        )
    alerts: list[ConsoleAlert] = []
    for ledger in ledgers:
        if ledger.drift_ratio is not None and abs(ledger.drift_ratio) > 0.10:
            alerts.append(
                ConsoleAlert(
                    severity="warning",
                    title=f"Ledger drift on {ledger.node_name}",
                    detail=f"Measured memory differs by {abs(ledger.drift_ratio):.1%}.",
                    target_id=str(ledger.node_id),
                )
            )
        if ledger.health.value != "healthy":
            alerts.append(
                ConsoleAlert(
                    severity="critical" if ledger.health.value == "unreachable" else "warning",
                    title=f"{ledger.node_name} is {ledger.health.value}",
                    detail=ledger.health_reason or "No current healthy sample is available.",
                    target_id=str(ledger.node_id),
                )
            )
    return ConsoleSnapshot(
        observed_at=observed_at,
        cursor=str(int(observed_at.timestamp() * 1000)),
        capabilities=ConsoleCapabilities(),
        cluster=cluster,
        ledgers=ledgers,
        alerts=alerts,
    )
