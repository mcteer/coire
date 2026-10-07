"""CPU evidence tests do not assert that a physical experiment was run."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from training_measurement_fixtures import experiment, observation

from coire_api.auth import Principal, PrincipalKind
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingConflict
from coire_core.models.auth import UserRole
from coire_core.models.training import (
    TrainingMeasurementCompletion,
    TrainingMeasurementPhase,
    TrainingMeasurementPrompt,
    TrainingMeasurementPromptSet,
    TrainingMeasurementRequest,
    TrainingResidentTarget,
)
from coire_core.settings import Settings
from coire_scheduler.training_guard import measurement_report_digest, memory_evidence_digest
from coire_scheduler.training_measurements import GatewayWorkloadDriver, build_report


def test_observed_envelope_hashes_all_components_and_hardware() -> None:
    row, dispatch = experiment()
    measured = observation(dispatch.commands[0])
    report = build_report(row, dispatch, [measured], [], Settings())
    assert report.memory_evidence is not None
    assert report.memory_evidence.resource_envelope.memory_bytes > measured.peak_footprint_bytes
    assert report.memory_evidence.resource_envelope.evidence_sha256 == memory_evidence_digest(
        report.memory_evidence
    )
    assert report.report_sha256 == measurement_report_digest(report)
    assert report.node_report_sha256 == [payload_digest(measured)]
    other = measured.model_copy(update={"optimizer_bytes": measured.optimizer_bytes + 1024})
    assert (
        build_report(row, dispatch, [other], [], Settings()).report_sha256 != report.report_sha256
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"swap_growth_bytes": 1},
        {"thermal_ok": False},
        {"completed_updates": 1},
        {"hardware_sha256": "b" * 64},
        {"request_sha256": "b" * 64},
        {"peak_footprint_bytes": 20 * 1024**3},
    ],
)
def test_no_approval_from_unsafe_partial_or_substituted_observations(
    changes: dict[str, object],
) -> None:
    row, dispatch = experiment()
    with pytest.raises(TrainingConflict):
        build_report(
            row,
            dispatch,
            [observation(dispatch.commands[0]).model_copy(update=changes)],
            [],
            Settings(),
        )


def test_every_declared_node_is_required() -> None:
    row, dispatch = experiment()
    with pytest.raises(TrainingConflict):
        build_report(row, dispatch, [], [], Settings())


@pytest.mark.parametrize("unreachable", [False, True])
async def test_stop_rejection_rebinds_prepare_but_transport_loss_keeps_unknown(
    unreachable: bool,
) -> None:
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_core.models.training_node import (
        TrainingMeasurementPrepare,
        TrainingPrepared,
        TrainingStopReceipt,
        TrainingStopRequest,
    )
    from coire_scheduler.training_measurements import TrainingMeasurementExecutor

    _, dispatch = experiment()
    probe = dispatch.commands[0]

    class Node:
        def __init__(self) -> None:
            self.stops: list[TrainingStopRequest] = []
            self.prepares: list[TrainingMeasurementPrepare] = []

        async def stop(self, command: TrainingStopRequest) -> TrainingStopReceipt:
            self.stops.append(command)
            if len(self.stops) == 1:
                raise NodeError(
                    NodeErrorKind.UNREACHABLE if unreachable else NodeErrorKind.CONFLICT,
                    probe.prepare.node,
                )
            return TrainingStopReceipt(
                attempt_id=command.attempt_id,
                fence=command.fence,
                node=command.node,
                stopped=True,
                observed_at=datetime.now(UTC),
            )

        async def prepare(self, command: TrainingMeasurementPrepare) -> TrainingPrepared:
            self.prepares.append(command)
            raise NodeError(NodeErrorKind.CONFLICT, command.prepare.node)

    node = Node()
    executor = TrainingMeasurementExecutor(Settings(), node)  # type: ignore[arg-type]
    proof = await executor.stop_probe(probe)
    if unreachable:
        assert proof is None and node.prepares == [] and len(node.stops) == 1
    else:
        assert proof is not None and proof.stopped
        assert node.prepares == [probe]
        assert len(node.stops) == 2 and node.stops[0] == node.stops[1]


def test_embedded_prompts_and_explicit_null_are_part_of_current_canonical_evidence() -> None:
    row, dispatch = experiment()
    request = TrainingMeasurementRequest.model_validate(row.request)
    assert request.model_dump(mode="json")["prompts"] is None
    observed = observation(dispatch.commands[0])
    before = build_report(row, dispatch, [observed], [], Settings())
    with_prompts = request.model_copy(
        update={
            "prompts": TrainingMeasurementPromptSet(
                prompts=[TrainingMeasurementPrompt(text="bounded CPU fixture", input_tokens=4000)]
            )
        }
    )
    assert payload_digest(with_prompts) != payload_digest(request)
    row.request = with_prompts.model_dump(mode="json")
    after = build_report(row, dispatch, [observed], [], Settings())
    assert before.memory_evidence == after.memory_evidence
    assert before.node_report_sha256 == after.node_report_sha256
    assert before.report_sha256 != after.report_sha256


@pytest.mark.parametrize("seconds,count,failures", [(899, 100, 0), (900, 99, 0), (900, 100, 1)])
def test_coexistence_duration_floor_is_per_target(seconds: int, count: int, failures: int) -> None:
    row, dispatch = experiment()
    req = TrainingMeasurementRequest.model_validate(row.request)
    target = {
        "instance_id": uuid.uuid4(),
        "target": {**req.spec.model.model_dump(mode="json"), "base_manifest_sha256": "a" * 64},
    }
    req.mode = "coexistence"
    req.resident_targets = [
        type(req)
        .model_validate({**req.model_dump(mode="json"), "resident_targets": [target]})
        .resident_targets[0]
    ]
    row.request = req.model_dump(mode="json")
    probe = dispatch.commands[0]
    probe.mode, probe.resident_targets = "coexistence", req.resident_targets
    measured = observation(probe)
    now = datetime.now(UTC)
    phases = [
        TrainingMeasurementPhase(
            phase=phase,
            workload_sha256=req.workload.sha256,
            started_at=now - timedelta(seconds=seconds),
            finished_at=now,
            samples={req.resident_targets[0].instance_id: [0.1] * count},
            failures=failures,
        )
        for phase in ("baseline", "mixed")
    ]
    with pytest.raises((TrainingConflict, ValidationError)):
        build_report(row, dispatch, [measured], phases, Settings())


@pytest.mark.parametrize("substitute", [False, True, "timeout"])
async def test_gateway_driver_counts_completed_exact_streams_only(
    monkeypatch: pytest.MonkeyPatch,
    substitute: bool | str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import coire_scheduler.training_measurements as module

    # Accelerate the CPU control-flow test; no generated evidence is approved.
    monkeypatch.setattr(module, "PHASE_SECONDS", 0.05)
    row, _ = experiment()
    request = TrainingMeasurementRequest.model_validate(row.request)
    target = TrainingResidentTarget.model_validate(
        {
            "instance_id": uuid.uuid4(),
            "target": {
                **request.spec.model.model_dump(mode="json"),
                "base_manifest_sha256": "a" * 64,
            },
        }
    )
    request.mode, request.resident_targets = "coexistence", [target]
    prompts = TrainingMeasurementPromptSet(
        prompts=[TrainingMeasurementPrompt(text="frozen prompt", input_tokens=4000)]
    )
    request.workload.sha256 = prompts.canonical_sha256()
    request.workload.arrival_interval_ms = 10
    principal = Principal(kind=PrincipalKind.USER, user_id=row.owner_user_id, role=UserRole.ADMIN)
    calls = []

    async def generate(
        owner: Principal,
        identity: uuid.UUID,
        resident: TrainingResidentTarget,
        prompt: TrainingMeasurementPrompt,
        output: int,
    ) -> TrainingMeasurementCompletion:
        assert owner == principal and identity == row.id and resident == target
        calls.append(prompt.text)
        if substitute == "timeout":
            import asyncio

            await asyncio.sleep(1)
        return TrainingMeasurementCompletion(
            instance_id=uuid.uuid4() if substitute else resident.instance_id,
            target=resident.target,
            first_token_seconds=0.1,
            input_tokens=prompt.input_tokens,
            output_tokens=output,
        )

    phase = await GatewayWorkloadDriver(generate).phase(
        principal, request, prompts, "baseline", measurement_id=row.id
    )
    assert calls and set(calls) == {"frozen prompt"}
    if substitute:
        assert phase.samples[target.instance_id] == []
        if substitute == "timeout":
            assert phase.failures > len(calls)
            reasons = {getattr(record, "reason", None) for record in caplog.records}
            assert {"concurrency_busy", "completion_timeout"} <= reasons
        else:
            assert phase.failures == len(calls)
            assert all(
                getattr(record, "reason", None) == "identity_mismatch" for record in caplog.records
            )
        assert "frozen prompt" not in caplog.text
    else:
        assert len(phase.samples[target.instance_id]) == len(calls) and not phase.failures
