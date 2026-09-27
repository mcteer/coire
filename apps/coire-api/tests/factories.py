"""Small deterministic console contract factories shared by focused tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from coire_core.models.console import ActivityItem, ActivityKind, ConsoleSnapshot
from coire_core.models.instance import ClusterState


def cluster_state() -> ClusterState:
    return ClusterState(observed_at=datetime(2026, 9, 1, tzinfo=UTC), nodes=[], instances=[])


def console_snapshot(**overrides: object) -> ConsoleSnapshot:
    values: dict[str, object] = {
        "observed_at": datetime(2026, 9, 1, tzinfo=UTC),
        "cursor": "1",
        "cluster": cluster_state(),
        "ledgers": [],
    }
    values.update(overrides)
    return ConsoleSnapshot.model_validate(values)


def activity_item(**overrides: object) -> ActivityItem:
    values: dict[str, object] = {
        "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
        "kind": ActivityKind.JOB,
        "owner": "platform",
        "target": "tiny-model",
        "state": "pull",
        "started_at": datetime(2026, 9, 1, tzinfo=UTC),
        "elapsed_seconds": 1,
        "progress_percent": 50,
        "can_stop": True,
    }
    values.update(overrides)
    return ActivityItem.model_validate(values)
