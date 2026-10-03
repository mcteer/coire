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
from coire_core.models.registry import EngineBackend, VisualCapability
from coire_core.settings import Settings
from coire_scheduler import acquisition as scheduler_acquisition
from coire_scheduler.acquisition import (
    node_job_id,
    replication_source_stage,
    require_validated_backend,
    reusable_stage_results,
)
from coire_scheduler.main import acquisition_dispatch_id


def test_node_job_ids_are_deterministic_per_workflow_stage() -> None:
    workflow = uuid.uuid4()
    first = node_job_id(workflow, AcquisitionStage.CONVERT)
    assert first == node_job_id(workflow, AcquisitionStage.CONVERT)
    assert first != node_job_id(workflow, AcquisitionStage.VALIDATE)
    assert first != node_job_id(uuid.uuid4(), AcquisitionStage.CONVERT)
    assert first != node_job_id(workflow, AcquisitionStage.CONVERT, attempt=2)


def test_acquisition_retry_gets_new_durable_workflow_id() -> None:
    workflow = uuid.uuid4()
    assert acquisition_dispatch_id(workflow, 1) == str(workflow)
    assert acquisition_dispatch_id(workflow, 2) != str(workflow)
    assert acquisition_dispatch_id(workflow, 2) == acquisition_dispatch_id(workflow, 2)
    assert acquisition_dispatch_id(workflow, 3) != acquisition_dispatch_id(workflow, 2)


def test_retry_replays_stale_mlx_noop_for_existing_variant() -> None:
    source = "model.mcp-acceptance"
    assert not reusable_stage_results(AcquisitionStage.CONVERT, source, [{"operation": "noop"}])
    assert reusable_stage_results(
        AcquisitionStage.CONVERT,
        source,
        [{"operation": "noop"}, {"manifest_sha256": "a" * 64}],
    )
    assert reusable_stage_results(AcquisitionStage.CONVERT, None, [{"operation": "noop"}])


def test_replication_uses_materialized_variant_when_pull_was_skipped() -> None:
    assert replication_source_stage(True, "model.mcp-acceptance") is AcquisitionStage.CONVERT
    assert replication_source_stage(True, None) is AcquisitionStage.PULL
    assert replication_source_stage(False, None) is AcquisitionStage.CONVERT


def test_visual_publication_requires_matching_backend_and_measured_capability() -> None:
    result = {
        "validator_version": "v1",
        "backend": EngineBackend.MLX_VLM.value,
        "smoke": "pass",
        "perplexity_outcome": "not_comparable",
        "template": "not_applicable",
        "tolerance": 0.1,
        "validated": True,
        "created_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(RuntimeError):
        require_validated_backend(result, EngineBackend.MLX_VLM)
    visual = VisualCapability(
        verified=True, max_images=1, max_image_pixels=256, max_encoded_bytes=80
    )
    measured = {**result, "visual_input": visual.model_dump(mode="json")}
    assert require_validated_backend(measured, EngineBackend.MLX_VLM).visual_input == visual
    with pytest.raises(RuntimeError):
        require_validated_backend(measured, EngineBackend.MLX_LM)
    with pytest.raises(RuntimeError):
        require_validated_backend({**measured, "validated": False}, EngineBackend.MLX_VLM)


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


async def test_existing_mlx_variant_materializes_new_slug_before_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    submitted = AsyncMock(
        return_value=JobStatus(
            job_id=uuid.uuid4(),
            kind=JobKind.CONVERT,
            slug="model.fabric-acceptance",
            stage=JobStage.DONE,
            manifest_sha256="a" * 64,
            started_at=now,
            updated_at=now,
        ).model_dump(mode="json")
    )
    held = AsyncMock()
    released = AsyncMock()
    finished = AsyncMock()
    monkeypatch.setattr(scheduler_acquisition, "_submit_command", submitted)
    monkeypatch.setattr(scheduler_acquisition, "_hold_conversion_memory", held)
    monkeypatch.setattr(scheduler_acquisition, "_release_conversion_memory", released)
    monkeypatch.setattr(scheduler_acquisition, "_finish_stage", finished)
    workflow_id = uuid.uuid4()
    context = {
        "origin": "coire-edge-b",
        "origin_id": uuid.uuid4(),
        "attempt": 1,
        "memory_estimate_bytes": 1024,
        "total_bytes": 2048,
        "repo_id": "test/model",
        "revision": "a" * 40,
        "variant_id": str(uuid.uuid4()),
        "source_variant_slug": "model.mcp-acceptance",
        "variant_slug": "model.fabric-acceptance",
        "recipe": {"name": "fabric-acceptance", "precision": "4bit", "bits": 4},
    }

    await scheduler_acquisition._run_command_stage(
        workflow_id, AcquisitionStage.CONVERT, context, True, "model.raw"
    )

    assert submitted.await_args is not None
    payload = submitted.await_args.kwargs["payload"]
    assert payload["source_slug"] == "model.mcp-acceptance"
    assert payload["target_slug"] == "model.fabric-acceptance"
    assert payload["dequantize"] is True
    held.assert_awaited_once()
    released.assert_awaited_once()
    finished.assert_awaited_once()
