from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_api.registry.acquisition_executor import AcquisitionCommandExecutor
from coire_core.models.acquisition import AcquisitionStage
from coire_core.models.jobs import JobKind, JobStage, JobStatus
from coire_core.settings import Settings
from coire_scheduler.acquisition import node_job_id


def test_node_job_ids_are_deterministic_per_workflow_stage() -> None:
    workflow = uuid.uuid4()
    first = node_job_id(workflow, AcquisitionStage.CONVERT)
    assert first == node_job_id(workflow, AcquisitionStage.CONVERT)
    assert first != node_job_id(workflow, AcquisitionStage.VALIDATE)
    assert first != node_job_id(uuid.uuid4(), AcquisitionStage.CONVERT)
    assert first != node_job_id(workflow, AcquisitionStage.CONVERT, attempt=2)


@pytest.mark.asyncio
async def test_wait_tolerates_initial_node_job_visibility_lag() -> None:
    executor = AcquisitionCommandExecutor(
        cast(Settings, SimpleNamespace(acquisition_poll_interval_s=0.001))
    )
    get_job = AsyncMock(
        side_effect=[
            NodeError(NodeErrorKind.NOT_FOUND, "coire-edge-a", status=404),
            JobStatus(
                job_id=uuid.uuid4(),
                kind=JobKind.PULL,
                slug="model",
                stage=JobStage.DONE,
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
        ]
    )
    client = cast(NodeClient, SimpleNamespace(get_job=get_job))

    result = await executor._wait(client, "coire-edge-a", uuid.uuid4())

    assert result["stage"] == JobStage.DONE.value
    assert get_job.await_count == 2
