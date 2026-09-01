"""Authoritative, label-scoped orphan detection and cleanup."""

from __future__ import annotations

from coire_core.models.runs import RunReconcileRequest, RunReconcileResult
from coire_node.runs import RunManager


class RunReconciler:
    def __init__(self, manager: RunManager) -> None:
        self.manager = manager

    async def reconcile(self, request: RunReconcileRequest) -> RunReconcileResult:
        observations = await self.manager.observations()
        managed_run_ids = await self.manager.managed_run_ids()
        orphan_ids = sorted(
            managed_run_ids - set(request.authoritative_run_ids),
            key=str,
        )
        reaped = []
        if request.reap_orphans:
            for run_id in orphan_ids:
                await self.manager.remove(run_id, kill=True)
                reaped.append(run_id)
        return RunReconcileResult(
            observations=observations,
            orphan_run_ids=orphan_ids,
            reaped_run_ids=reaped,
        )
