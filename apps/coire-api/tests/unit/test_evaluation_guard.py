"""Qualification binds complete measured evidence to a short lived exact profile."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coire_api.db import EvaluationCoexistenceProfileRow, EvaluationMeasurementRow, NodeRow
from coire_core.models.evaluation import (
    EvaluationLatency,
    EvaluationMeasurement,
    EvaluationMeasurementRequest,
    EvaluationResident,
    EvaluationSubject,
    EvaluationSubmission,
    EvaluationWorkload,
)
from coire_scheduler.evaluation_guard import fingerprint, matching_profile, qualified

FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


def report() -> EvaluationMeasurement:
    workload = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    now = datetime.now(UTC)
    resident = EvaluationResident(instance_id=uuid.UUID(int=51), target=workload.target.target)
    request = EvaluationMeasurementRequest(
        evaluation=EvaluationSubmission(
            suite_id=workload.suite.suite_id,
            suite_version=workload.suite.version,
            subjects=[
                EvaluationSubject(
                    model_id=workload.target.target.model_id,
                    variant_id=workload.target.target.variant_id,
                )
            ],
        ),
        node="coire-edge-a",
        resident_targets=[resident],
        prompt_set_sha256="a" * 64,
    )
    value = EvaluationMeasurement(
        id=uuid.UUID(int=52),
        version=1,
        state="succeeded",
        request=request,
        targets=[
            EvaluationLatency(
                instance_id=resident.instance_id,
                baseline_requests=100,
                mixed_requests=100,
                baseline_p95_seconds=1.0,
                mixed_p95_seconds=1.2,
                gateway_p95_seconds=0.01,
                failures=0,
            )
        ],
        thermal_ok=True,
        created_at=now,
        valid_until=now + timedelta(hours=24),
        profile_sha256="b" * 64,
        report_sha256="0" * 64,
    )
    digest = hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json", exclude={"report_sha256"}),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return value.model_copy(update={"report_sha256": digest})


def test_qualification_rejects_expired_and_overlong_profiles() -> None:
    value = report()
    assert qualified(value, now=value.created_at)
    assert value.valid_until is not None
    assert not qualified(value, now=value.valid_until)
    assert not qualified(
        value.model_copy(update={"valid_until": value.created_at + timedelta(hours=25)}),
        now=value.created_at,
    )


def test_profile_fingerprint_changes_with_generation_and_runtime() -> None:
    workload = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    node = NodeRow(
        name="coire-edge-a", memory_total_bytes=128 * 1024**3, gpu_cores=60, agent_version="1"
    )
    original = fingerprint(workload.suite, [workload.target], [], node)
    changed = workload.suite.model_copy(
        update={"generation": workload.suite.generation.model_copy(update={"seed": 7})}
    )
    assert fingerprint(changed, [workload.target], [], node) != original
    target = workload.target.model_copy(
        update={"runtime": workload.target.runtime.model_copy(update={"engine_version": "changed"})}
    )
    assert fingerprint(workload.suite, [target], [], node) != original


@pytest.mark.asyncio
async def test_profile_refuses_tampered_report_even_with_unchanged_claimed_digest() -> None:
    value = report()
    profile = EvaluationCoexistenceProfileRow(
        measurement_id=value.id,
        fingerprint_sha256=value.profile_sha256,
        report_sha256=value.report_sha256,
        expires_at=value.valid_until,
    )
    document = value.model_dump(mode="json")
    document["targets"][0]["mixed_p95_seconds"] = 1.1
    row = EvaluationMeasurementRow(
        id=value.id, state="succeeded", report=document, report_sha256=value.report_sha256
    )
    session = AsyncMock()
    session.scalars.return_value.all = lambda: [profile]
    session.get.return_value = row
    assert value.profile_sha256 is not None
    assert await matching_profile(session, value.profile_sha256, now=value.created_at) is None


@pytest.mark.parametrize("status", ["qualified", "expired", "invalidated"])
async def test_measurement_view_exposes_profile_validity_without_changing_report_digest(
    status: str,
) -> None:
    from coire_api.evaluation.measurements import detail

    value = report()
    row = EvaluationMeasurementRow(
        id=value.id, state="succeeded", report=value.model_dump(mode="json")
    )
    profile = EvaluationCoexistenceProfileRow(
        measurement_id=value.id,
        fingerprint_sha256=value.profile_sha256,
        report_sha256=value.report_sha256,
        expires_at=value.created_at - timedelta(seconds=1)
        if status == "expired"
        else value.valid_until,
        invalidated_at=value.created_at if status == "invalidated" else None,
        invalidated_reason="telemetry_stale" if status == "invalidated" else None,
    )
    session = AsyncMock()
    session.scalar.return_value = profile
    result = await detail(session, row)
    assert result.profile_status == status and result.report_sha256 == value.report_sha256
    assert result.profile_invalidated_at == profile.invalidated_at


def test_training_input_work_requires_its_measured_resource_shape(tmp_path: Path) -> None:
    from evaluation_input_fixtures import staged_workload

    work = staged_workload(tmp_path)
    node = NodeRow(
        name="coire-edge-a", memory_total_bytes=128 * 1024**3, gpu_cores=60, agent_version="1"
    )
    no_inputs = fingerprint(work.suite, [work.target], [], node)
    measured_inputs = fingerprint(work.suite, [work.target], [], node, training=work.training)
    assert measured_inputs != no_inputs
    assert work.training is not None
    changed = work.training.model_copy(update={"completed_update": 2})
    assert fingerprint(work.suite, [work.target], [], node, training=changed) != measured_inputs
