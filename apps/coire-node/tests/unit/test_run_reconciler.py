from __future__ import annotations

import uuid
from datetime import UTC, datetime

from coire_core.models.runs import RunContainerObservation, RunReconcileRequest
from coire_node.run_reconciler import RunReconciler


class Manager:
    def __init__(self, observations: list[RunContainerObservation]) -> None:
        self.items = observations
        self.removed: list[tuple[uuid.UUID, bool]] = []

    async def observations(self) -> list[RunContainerObservation]:
        return self.items

    async def remove(self, run_id: uuid.UUID, *, kill: bool = False) -> None:
        self.removed.append((run_id, kill))


async def test_reconciler_reaps_only_non_authoritative_labeled_runs() -> None:
    known, orphan = uuid.uuid4(), uuid.uuid4()
    manager = Manager(
        [
            RunContainerObservation(
                run_id=value,
                container_id=f"container-{value}",
                state="running",
                observed_at=datetime.now(UTC),
            )
            for value in (known, orphan)
        ]
    )
    result = await RunReconciler(manager).reconcile(  # type: ignore[arg-type]
        RunReconcileRequest(authoritative_run_ids=frozenset({known}))
    )
    assert result.orphan_run_ids == [orphan]
    assert result.reaped_run_ids == [orphan]
    assert manager.removed == [(orphan, True)]


async def test_reconciler_can_report_without_reaping() -> None:
    orphan = uuid.uuid4()
    manager = Manager(
        [
            RunContainerObservation(
                run_id=orphan,
                container_id="container",
                state="exited",
                observed_at=datetime.now(UTC),
            )
        ]
    )
    result = await RunReconciler(manager).reconcile(  # type: ignore[arg-type]
        RunReconcileRequest(reap_orphans=False)
    )
    assert result.orphan_run_ids == [orphan]
    assert result.reaped_run_ids == []
    assert manager.removed == []
