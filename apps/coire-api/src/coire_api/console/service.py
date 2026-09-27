"""Pure assembly boundary for the admin console's reconciliable snapshot."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import UTC, datetime

from fastapi import Request
from opentelemetry import metrics

from coire_api.auth import CurrentAdmin
from coire_api.db import session_scope
from coire_api.deps import SessionDep, SettingsDep
from coire_api.placement.service import project_ledgers
from coire_api.routes.instances import project_cluster_state
from coire_core.models.console import (
    ConsoleAlert,
    ConsoleCapabilities,
    ConsoleSnapshot,
    CoreHostCapacity,
)

_projections = metrics.get_meter("coire.api.console").create_counter(
    "coire_console_projections_total", unit="1"
)


class SnapshotCache:
    """One copy-safe projection per app and short refresh window."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.expires_at = 0.0
        self.snapshot: ConsoleSnapshot | None = None


async def cached_snapshot(
    request: Request, principal: CurrentAdmin, settings: SettingsDep
) -> ConsoleSnapshot:
    cache: SnapshotCache | None = getattr(request.app.state, "console_snapshot_cache", None)
    if cache is None:
        cache = SnapshotCache()
        request.app.state.console_snapshot_cache = cache
    async with cache.lock:
        if cache.snapshot is None or time.monotonic() >= cache.expires_at:
            async with session_scope() as session:
                snapshot = await project_snapshot(request, principal, session, settings)
            _projections.add(1)
            cache.snapshot = snapshot.model_copy(deep=True)
            cache.expires_at = time.monotonic() + settings.console_snapshot_interval_s
        return cache.snapshot.model_copy(deep=True)


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
    return CoreHostCapacity(
        host_name=os.uname().nodename,
        health="healthy",
        memory_total_bytes=max(memory_total, memory_free),
        memory_free_bytes=memory_free,
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
        cpu_percent=None,
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
    ledgers = await project_ledgers(session)
    cluster = await project_cluster_state(session, settings, ledgers)
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
