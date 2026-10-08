"""Measured qualification requires complete exact-resident samples and fails closed."""

import uuid
from pathlib import Path

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import (
    EvaluationMeasurementRequest,
    EvaluationProbeCompletion,
    EvaluationProbePrepared,
    EvaluationProbePrompt,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkload,
)
from coire_scheduler.evaluation_measurements import percentile, sample

FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


def test_nearest_rank_p95_uses_every_sample_and_rejects_nonfinite_values() -> None:
    from coire_core.errors import EvaluationConflict

    assert percentile([float(value) for value in range(100)]) == 94
    with pytest.raises(EvaluationConflict):
        percentile([1.0, float("nan")])


@pytest.mark.parametrize("fail_at", [None, 50])
async def test_complete_counts_or_explicit_failure(
    monkeypatch: pytest.MonkeyPatch, fail_at: int | None
) -> None:
    import coire_scheduler.evaluation_measurements as module

    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    identity = uuid.uuid4()
    resident = EvaluationResident(instance_id=uuid.uuid4(), target=work.target.target)
    request = EvaluationMeasurementRequest(
        evaluation=EvaluationSubmission(
            suite_id=work.suite.suite_id,
            suite_version=1,
            subjects=[
                EvaluationSubject(
                    model_id=resident.target.model_id, variant_id=resident.target.variant_id
                )
            ],
        ),
        node="coire-edge-a",
        resident_targets=[resident],
        prompt_set_sha256="a" * 64,
        arrival_interval_ms=1,
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4(), role=UserRole.ADMIN)
    probes = EvaluationProbePrepared(
        measurement_id=identity,
        prompt_set_sha256="a" * 64,
        prompts=[
            EvaluationProbePrompt(
                id="probe-0", text="fixture", tokens_by_instance={resident.instance_id: 10}
            )
        ],
    )
    calls = 0

    async def check(
        measurement: uuid.UUID, ordinal: int | None = None
    ) -> tuple[Principal, EvaluationMeasurementRequest, EvaluationProbePrepared]:
        assert measurement == identity and ordinal == 1
        return principal, request, probes

    async def generate(*args: object) -> EvaluationProbeCompletion:
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise ConnectionError("fixture failure")
        return EvaluationProbeCompletion(
            instance_id=resident.instance_id,
            first_token_seconds=1,
            gateway_seconds=0.01,
            input_tokens=10,
            output_tokens=5,
        )

    monkeypatch.setattr(module, "check", check)
    monkeypatch.setattr(module, "generate", generate)
    if fail_at is not None:
        with pytest.raises(ExceptionGroup):
            await sample(identity, 1)
        assert calls == 50
    else:
        measured = await sample(identity, 1)
        assert calls == measured[str(resident.instance_id)].count == 100
        assert measured[str(resident.instance_id)].p95 == 1


@pytest.mark.parametrize("change", [None, "identity", "digest", "resident", "text"])
def test_serving_probe_receipt_binds_exact_measurement_prompts_and_residents(
    change: str | None,
) -> None:
    from coire_api.evaluation.measurements import validate_probe_receipt
    from coire_core.errors import EvaluationConflict
    from coire_core.evaluation_suites.measurement import PROMPTS, prompt_digest

    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    identity = uuid.uuid4()
    resident = EvaluationResident(instance_id=uuid.uuid4(), target=work.target.target)
    probes = EvaluationProbePrepared(
        measurement_id=identity,
        prompt_set_sha256=prompt_digest(),
        prompts=[
            EvaluationProbePrompt(
                id=f"probe-{index}", text=text, tokens_by_instance={resident.instance_id: 10}
            )
            for index, text in enumerate(PROMPTS)
        ],
    )
    if change == "identity":
        probes = probes.model_copy(update={"measurement_id": uuid.uuid4()})
    elif change == "digest":
        probes = probes.model_copy(update={"prompt_set_sha256": "f" * 64})
    elif change in {"resident", "text"}:
        probes = probes.model_copy(
            update={
                "prompts": [
                    item.model_copy(
                        update={"tokens_by_instance": {uuid.uuid4(): 10}}
                        if change == "resident"
                        else {"text": "foreign"}
                    )
                    for item in probes.prompts
                ]
            }
        )
    if change is None:
        validate_probe_receipt(probes, identity, [resident])
    else:
        with pytest.raises(EvaluationConflict):
            validate_probe_receipt(probes, identity, [resident])


@pytest.mark.parametrize("unreachable", [False, True])
async def test_waiting_phase_keeps_real_serving_evidence_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, unreachable: bool
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, Mock

    import coire_scheduler.evaluation_measurements as module
    from coire_api.db import AgentRunRow, EvaluationMeasurementRow, EvaluationRunRow
    from coire_core.models.runs import AgentRunState

    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    identity = uuid.uuid4()
    resident = EvaluationResident(instance_id=uuid.uuid4(), target=work.target.target)
    request = EvaluationMeasurementRequest(
        evaluation=EvaluationSubmission(
            suite_id=work.suite.suite_id,
            suite_version=1,
            subjects=[
                EvaluationSubject(
                    model_id=resident.target.model_id, variant_id=resident.target.variant_id
                )
            ],
        ),
        node="coire-edge-a",
        resident_targets=[resident],
        prompt_set_sha256="a" * 64,
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4(), role=UserRole.ADMIN)
    probes = EvaluationProbePrepared(
        measurement_id=identity,
        prompt_set_sha256="a" * 64,
        prompts=[
            EvaluationProbePrompt(
                id="probe-0", text="fixture", tokens_by_instance={resident.instance_id: 10}
            )
        ],
    )
    ready = False
    calls = 0
    attempt = Mock(agent_run_id=uuid.uuid4())

    async def check(
        *args: object,
    ) -> tuple[Principal, EvaluationMeasurementRequest, EvaluationProbePrepared]:
        return principal, request, probes

    async def generate(*args: object) -> EvaluationProbeCompletion:
        nonlocal ready, calls
        calls += 1
        assert args[1:] == (
            principal,
            identity,
            resident,
            probes.prompts[0],
            request.max_output_tokens,
        )
        if unreachable:
            raise ConnectionError("serving evidence unavailable")
        ready = True
        return EvaluationProbeCompletion(
            instance_id=resident.instance_id,
            first_token_seconds=0.2,
            gateway_seconds=0.01,
            input_tokens=10,
            output_tokens=5,
        )

    async def get(kind: object, *args: object) -> Mock:
        if kind is EvaluationMeasurementRow:
            return Mock(execution={"run_id": "fixture"})
        if kind is EvaluationRunRow:
            return Mock(state="running")
        assert kind is AgentRunRow and ready
        return Mock(state=AgentRunState.RUNNING)

    async def scalar(*args: object) -> Mock | None:
        return attempt if ready else None

    session = AsyncMock()
    session.get.side_effect = get
    session.scalar.side_effect = scalar

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncMock]:
        yield session

    monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(module, "check", check)
    monkeypatch.setattr(module, "generate", generate)
    if unreachable:
        with pytest.raises(ConnectionError):
            await module.wait_phase(identity, 1)
    else:
        assert await module.wait_phase(identity, 1) is attempt
    assert calls == 1
