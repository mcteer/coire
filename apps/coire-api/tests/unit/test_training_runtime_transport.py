"""Shared typed node receipts and independently owned disabled-feature lifecycle."""

import asyncio
import uuid
from typing import Any

import pytest
from training_measurement_fixtures import DIGEST, experiment

from coire_api.training.runtime import TrainingRuntimeWorker
from coire_api.training_executor import TrainingNodeClient
from coire_core.errors import TrainingConflict
from coire_core.models.training_node import CheckpointCommitAcknowledgement, NodeTrainingStatus
from coire_core.settings import Settings


@pytest.mark.parametrize("wrong_node", [False, True])
async def test_checkpoint_commit_uses_typed_200_status(
    monkeypatch: pytest.MonkeyPatch, wrong_node: bool
) -> None:
    _, dispatch = experiment()
    prepare = dispatch.commands[0].prepare
    acknowledgement = CheckpointCommitAcknowledgement.model_validate(
        {
            **prepare.model_dump(
                exclude={
                    "resolved",
                    "reservation_id",
                    "disk_reservation_id",
                    "resume_manifest_sha256",
                    "resume_checkpoint_id",
                    "collective",
                }
            ),
            "checkpoint_id": uuid.uuid4(),
            "manifest_sha256": DIGEST,
            "update": 1,
        }
    )
    status = NodeTrainingStatus(
        job_id=prepare.job_id,
        attempt_id=prepare.attempt_id,
        fence=prepare.fence,
        node="coire-edge-b" if wrong_node else prepare.node,
        liveness="running",
        update=1,
        lease_expires_at=prepare.lease_expires_at,
    )
    calls: list[tuple[str, str, str, object]] = []

    async def call(method: str, node: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        calls.append((method, node, path, kwargs["expect"]))
        return 200, status.model_dump(mode="json")

    client = TrainingNodeClient(Settings())
    monkeypatch.setattr(client, "_call", call)
    try:
        if wrong_node:
            with pytest.raises(TrainingConflict):
                await client.acknowledge_checkpoint(acknowledgement)
        else:
            assert await client.acknowledge_checkpoint(acknowledgement) == status
        assert calls == [
            (
                "POST",
                prepare.node,
                f"/node/training/attempts/{prepare.attempt_id}/checkpoint-commit",
                (200,),
            )
        ]
    finally:
        await client.aclose()


async def test_disabled_worker_owns_metrics_and_closes_client_after_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    worker = TrainingRuntimeWorker(Settings(training_enabled=False))
    started = asyncio.Event()

    async def start_controller() -> None:
        order.append("controller-start")

    async def stop_controller() -> None:
        order.append("controller-stop")

    async def poll(stop: asyncio.Event) -> None:
        order.append("metrics-start")
        started.set()
        try:
            await stop.wait()
        finally:
            order.append("metrics-stop")

    original_close = worker.client.aclose

    async def close() -> None:
        order.append("client-close")
        await original_close()

    monkeypatch.setattr(worker.controller, "start", start_controller)
    monkeypatch.setattr(worker.controller, "stop", stop_controller)

    async def no_op() -> None:
        pass

    monkeypatch.setattr(worker.components, "start", no_op)
    monkeypatch.setattr(worker.components, "stop", no_op)
    monkeypatch.setattr(worker, "_poll_measurement_recovery", no_op)
    monkeypatch.setattr(worker, "_poll_promotions", no_op)
    monkeypatch.setattr(worker.client, "aclose", close)
    monkeypatch.setattr("coire_scheduler.training_metrics.poll_training_metrics", poll)
    await worker.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await worker.stop()
    assert order == [
        "controller-start",
        "metrics-start",
        "controller-stop",
        "metrics-stop",
        "client-close",
    ]


@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_registers_real_guard_and_measurement_lifecycle(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from coire_scheduler.training_controller import TrainingExecutorTransport
    from coire_scheduler.training_guard import guard_reason, resume_profile

    worker = TrainingRuntimeWorker(Settings(training_enabled=enabled))
    transport = worker.controller.transport
    assert isinstance(transport, TrainingExecutorTransport)
    assert transport._guard is guard_reason and transport._resume_profile is resume_profile
    assert worker.measurements.transport is worker.measurement_client
    assert worker.measurements.workload is not None
    order: list[str] = []

    async def no_op() -> None:
        pass

    async def start_measurements() -> None:
        order.append("measurement-start")

    async def stop_measurements() -> None:
        order.append("measurement-stop")

    original_close = worker.measurement_client.aclose

    async def close_measurements() -> None:
        order.append("measurement-close")
        await original_close()

    monkeypatch.setattr(worker.controller, "start", no_op)
    monkeypatch.setattr(worker.controller, "stop", no_op)
    monkeypatch.setattr(worker.components, "start", no_op)
    monkeypatch.setattr(worker.components, "stop", no_op)
    monkeypatch.setattr(worker, "_poll_measurement_recovery", no_op)
    monkeypatch.setattr(worker, "_poll_promotions", no_op)
    monkeypatch.setattr(worker.measurements, "start", start_measurements)
    monkeypatch.setattr(worker.measurements, "stop", stop_measurements)
    monkeypatch.setattr(worker.measurement_client, "aclose", close_measurements)
    await worker.start()
    await worker.stop()
    assert order == (["measurement-start"] if enabled else []) + [
        "measurement-stop",
        "measurement-close",
    ]


async def test_extraction_uses_real_shared_post_and_get_200_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from coire_core.models.training_node import (
        TrainingAdapterExtractionStatus,
        TrainingAdapterExtractRequest,
    )

    _, dispatch = experiment()
    prepare = dispatch.commands[0].prepare
    command = TrainingAdapterExtractRequest(
        command_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        checkpoint_id=uuid.uuid4(),
        checkpoint_manifest_sha256=DIGEST,
        job_id=prepare.job_id,
        attempt_id=prepare.attempt_id,
        fence=prepare.fence,
        node=prepare.node,
        resolved=prepare.resolved,
        disk_reservation_id=uuid.uuid4(),
        max_bytes=1024,
        deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    status = TrainingAdapterExtractionStatus(
        command_id=command.command_id,
        adapter_id=command.adapter_id,
        checkpoint_id=command.checkpoint_id,
        node=command.node,
        state="running",
    )
    calls: list[tuple[str, str]] = []

    async def call(method: str, node: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        assert node == command.node
        if method == "POST":
            assert TrainingAdapterExtractRequest.model_validate(kwargs["json"]) == command
        calls.append((method, path))
        return 200, status.model_dump(mode="json")

    client = TrainingNodeClient(Settings())
    monkeypatch.setattr(client, "_call", call)
    try:
        assert await client.extract_adapter(command) == status
        assert await client.adapter_extraction_status(command.node, command.command_id) == status
        assert calls == [
            ("POST", "/node/training/adapters/extractions"),
            ("GET", f"/node/training/adapters/extractions/{command.command_id}"),
        ]
    finally:
        await client.aclose()


async def test_inputs_transport_accepts_only_scoped_typed_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.models.training_node import TrainingInputsRequest, TrainingPrepared

    _, dispatch = experiment()
    prepare = dispatch.commands[0].prepare
    source = dispatch.sources[0]
    request = TrainingInputsRequest.model_validate(
        {
            **prepare.model_dump(
                exclude={
                    "resolved",
                    "reservation_id",
                    "disk_reservation_id",
                    "resume_checkpoint_id",
                    "resume_manifest_sha256",
                    "collective",
                }
            ),
            "sources": [
                {
                    **source.model_dump(mode="json"),
                    "grant": {
                        "grant_id": uuid.uuid4(),
                        "node": prepare.node,
                        "dataset_id": source.binding.dataset_id,
                        "source_sha256": DIGEST,
                        "max_bytes": 10,
                        "attempt_id": prepare.attempt_id,
                        "expires_at": prepare.lease_expires_at,
                        "secret": "transport-test-" * 3,
                    },
                }
            ],
        }
    )
    receipt = TrainingPrepared(
        attempt_id=prepare.attempt_id,
        fence=prepare.fence,
        node=prepare.node,
        reservation_id=prepare.reservation_id,
        runtime_sha256=DIGEST,
        ready=False,
        reason="analysis_pending",
    )

    async def call(method: str, node: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        assert method == "POST" and node == prepare.node
        assert path == f"/node/training/attempts/{prepare.attempt_id}/inputs"
        assert kwargs["expect"] == (202,)
        assert TrainingInputsRequest.model_validate(kwargs["json"]) == request
        return 202, receipt.model_dump(mode="json")

    client = TrainingNodeClient(Settings())
    monkeypatch.setattr(client, "_call", call)
    try:
        assert await client.deliver_training_inputs(request) == receipt
    finally:
        await client.aclose()
