"""Acceptance client verifies real typed seams; tests use no Studio or model."""

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import yaml
from pydantic import BaseModel
from training_measurement_fixtures import ATTEMPT, DIGEST, JOB, experiment

from coire_core.models.adapters import AdapterDetail, AdapterState
from coire_core.models.training import (
    CheckpointDetail,
    CheckpointPage,
    TrainingJobDetail,
    TrainingJobReceipt,
    TrainingJobState,
    TrainingMetricPage,
    TrainingMetricSample,
    TrainingSubmission,
    TrainingValidation,
)


@pytest.fixture
def driver() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/validate-sft-training.py"
    spec = importlib.util.spec_from_file_location("sft_acceptance_driver", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Protocol:
    def __init__(self, *, states: list[str] | None = None, verified: bool = False) -> None:
        _, dispatch = experiment()
        self.resolved = dispatch.commands[0].prepare.resolved
        self.submission = TrainingSubmission(
            source_yaml=yaml.safe_dump(self.resolved.spec.model_dump(mode="json")),
            source_kind="yaml",
        )
        self.states = states or ["running", "succeeded"]
        self.requests: list[httpx.Request] = []
        self.checkpoint_id = uuid.uuid4()
        model = self.resolved.spec.model
        self.adapter = AdapterDetail(
            id=uuid.uuid4(),
            model_id=model.model_id,
            base_variant_id=model.variant_id,
            slug="acceptance",
            selector=f"{model.model_id}@acceptance",
            state=AdapterState.READY,
            base_manifest_sha256=DIGEST,
            manifest_sha256=DIGEST,
            source_job_id=JOB,
            source_checkpoint_id=self.checkpoint_id,
            resolved_spec_sha256=DIGEST,
            parameterization=self.resolved.spec.parameterization.kind,
            verified=verified,
            evaluation_id=uuid.uuid4() if verified else None,
            version=1,
            created_at=datetime.now(UTC),
        )

    def reply(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer synthetic-personal-admin"
        self.requests.append(request)
        path = request.url.path
        now = datetime.now(UTC)
        result: BaseModel
        if path.endswith("/validate"):
            result = TrainingValidation(
                spec=self.resolved.spec,
                intent_sha256=DIGEST,
                resolved=self.resolved,
                ready_to_run=True,
            )
        elif path.endswith("/jobs"):
            result = TrainingJobReceipt(
                job_id=JOB,
                state=TrainingJobState.QUEUED,
                version=1,
                events_path=f"/api/v1/admin/training/jobs/{JOB}/events",
            )
        elif path.endswith("/" + JOB):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            result = TrainingJobDetail.model_validate(
                {
                    "id": JOB,
                    "version": 2,
                    "state": state,
                    "source_yaml": self.submission.source_yaml,
                    "source_sha256": DIGEST,
                    "intent_sha256": DIGEST,
                    "spec": self.resolved.spec,
                    "resolved": self.resolved,
                    "attempt_id": ATTEMPT,
                    "completed_update": self.resolved.spec.optim.updates
                    if state == "succeeded"
                    else 1,
                    "latest_checkpoint_id": self.checkpoint_id,
                    "adapter_id": self.adapter.id if state == "succeeded" else None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        elif path.endswith("/checkpoints"):
            result = CheckpointPage(
                items=[
                    CheckpointDetail(
                        id=self.checkpoint_id,
                        job_id=JOB,
                        attempt_id=ATTEMPT,
                        fence=1,
                        update=self.resolved.spec.optim.updates,
                        manifest_sha256=DIGEST,
                        total_bytes=3,
                        state="committed",
                        verified_nodes=["coire-edge-a", "coire-edge-b"],
                        created_at=now,
                    )
                ]
            )
        elif path.endswith("/metrics"):
            result = TrainingMetricPage(
                items=[
                    TrainingMetricSample(
                        job_id=JOB,
                        attempt_id=ATTEMPT,
                        update=self.resolved.spec.optim.updates,
                        kind=kind,
                        loss=0.5,
                        learning_rate=0.001,
                        tokens=1,
                        tokens_per_second=1,
                        updates_per_second=1,
                        footprint_bytes=100,
                        peak_bytes=100,
                        recorded_at=now,
                    )
                    for kind in ("train", "validation")
                ]
            )
        elif path.endswith("/" + str(self.adapter.id)):
            result = self.adapter
        elif path == "/v1/chat/completions":
            body = json.loads(request.content)
            assert body["model"] == self.adapter.selector
            assert body["coire_variant_id"] == str(self.adapter.base_variant_id)
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-chat",
                    "object": "chat.completion",
                    "created": 1,
                    "model": self.adapter.selector,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "private output"},
                        }
                    ],
                },
            )
        elif path.endswith(("/pause", "/resume", "/cancel")):
            operation = path.rsplit("/", 1)[1]
            assert json.loads(request.content) == {"expected_version": 2}
            if operation == "pause":
                self.states = ["paused"]
            elif operation == "resume":
                self.states = ["succeeded"]
            elif operation == "cancel":
                self.states = ["cancelled"]
            return httpx.Response(
                202,
                json={
                    "command_id": str(uuid.uuid4()),
                    "job_id": JOB,
                    "version": 3,
                    "state": "queued"
                    if operation == "resume"
                    else "pausing"
                    if operation == "pause"
                    else "cancelling",
                },
            )
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [None, "pause", "cancel"])
async def test_typed_recipe_control_checkpoint_adapter_and_gateway_paths(
    driver: ModuleType,
    operation: str | None,
) -> None:
    protocol = Protocol()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(protocol.reply),
        base_url="http://synthetic-control",
        headers={"Authorization": "Bearer synthetic-personal-admin"},
    ) as client:
        runner = driver.AcceptanceRunner(client, timeout_s=2, poll_s=0.001)
        result = await runner.run(
            protocol.submission,
            key="trial" * 25,
            pause_at_update=1 if operation == "pause" else None,
            cancel_at_update=1 if operation == "cancel" else None,
        )
        assert result["passed"] is True and result["job_id"] == JOB
        encoded = json.dumps(result)
        assert "private output" not in encoded and protocol.submission.source_yaml not in encoded
        assert "synthetic-personal-admin" not in encoded
        assert all(
            len(request.headers.get("idempotency-key", "")) <= 128 for request in protocol.requests
        )
        submissions = [
            request
            for request in protocol.requests
            if request.method == "POST" and request.url.path.endswith("/jobs")
        ]
        assert (
            len(submissions) == 2
            and submissions[0].headers["idempotency-key"]
            == submissions[1].headers["idempotency-key"]
        )
        if operation == "cancel":
            assert not any(
                request.url.path == "/v1/chat/completions" for request in protocol.requests
            )
        else:
            assert result["loss_samples"] == {"train": 1, "validation": 1}


@pytest.mark.asyncio
async def test_unperformed_cancel_and_auto_verified_adapter_cannot_pass(driver: ModuleType) -> None:
    for protocol, threshold in [
        (Protocol(states=["succeeded"]), 1),
        (Protocol(verified=True), None),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(protocol.reply),
            base_url="http://synthetic-control",
            headers={"Authorization": "Bearer synthetic-personal-admin"},
        ) as client:
            runner = driver.AcceptanceRunner(client, timeout_s=2, poll_s=0.001)
            with pytest.raises(driver.AcceptanceFailed):
                await runner.run(protocol.submission, key="trial", cancel_at_update=threshold)
            assert not any(
                request.url.path == "/v1/chat/completions" for request in protocol.requests
            )


def test_reports_are_private_external_and_never_overwrite(
    driver: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "metadata.json"
    driver.write_report(path, {"job_id": JOB, "passed": False})
    assert not path.stat().st_mode & 0o077
    with pytest.raises(FileExistsError):
        driver.write_report(path, {"passed": True})
    assert json.loads(path.read_text())["passed"] is False
