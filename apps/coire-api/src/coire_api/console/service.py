"""Pure assembly boundary for the admin console's reconciliable snapshot."""

from __future__ import annotations

import os
import shutil
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
    CoreHostCapacity,
)


def project_core_capacity(observed_at: datetime) -> CoreHostCapacity:
    """Return runtime-visible capacity with an explicit source boundary."""

    page_size = os.sysconf("SC_PAGE_SIZE")
    memory_total = int(page_size * os.sysconf("SC_PHYS_PAGES"))
    try:
        memory_free = int(page_size * os.sysconf("SC_AVPHYS_PAGES"))
    except (OSError, ValueError):
        # macOS does not expose Linux's available-pages sysconf key. Keep the field truthful
        # rather than substituting an unrelated estimate.
        memory_free = 0
    disk = shutil.disk_usage("/")
    try:
        cpu_percent: float | None = min(
            100.0,
            max(0.0, os.getloadavg()[0] * 100.0 / max(1, os.cpu_count() or 1)),
        )
    except (OSError, NotImplementedError):
        cpu_percent = None
    return CoreHostCapacity(
        host_name=os.uname().nodename,
        health="healthy",
        memory_total_bytes=max(memory_total, memory_free),
        memory_free_bytes=memory_free,
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
        cpu_percent=cpu_percent,
        observed_at=observed_at,
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
        core=project_core_capacity(observed_at),
        ledgers=ledgers,
        alerts=alerts,
    )
