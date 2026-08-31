from __future__ import annotations

import uuid

from coire_core.models.acquisition import AcquisitionStage
from coire_scheduler.acquisition import node_job_id


def test_node_job_ids_are_deterministic_per_workflow_stage() -> None:
    workflow = uuid.uuid4()
    first = node_job_id(workflow, AcquisitionStage.CONVERT)
    assert first == node_job_id(workflow, AcquisitionStage.CONVERT)
    assert first != node_job_id(workflow, AcquisitionStage.VALIDATE)
    assert first != node_job_id(uuid.uuid4(), AcquisitionStage.CONVERT)
    assert first != node_job_id(workflow, AcquisitionStage.CONVERT, attempt=2)
