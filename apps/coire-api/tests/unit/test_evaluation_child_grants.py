"""Internal evaluation grants cannot widen a phase target or allow tools/writes."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_api.db import AgentRunRow, EvaluationAttemptRow, EvaluationRunRow
from coire_api.run_tokens import InvalidRunToken, validate_run_scope
from coire_core.models.adapters import InferenceTarget
from coire_core.models.harness import TaskClass
from coire_core.models.runs import RunTokenScope


@pytest.mark.parametrize("change", ["suite", "phase", "target", "deadline", "pressure"])
def test_child_workload_cannot_diverge_from_frozen_parent(change: str) -> None:
    from coire_api.evaluation.execution import validate_phase_binding
    from coire_core.errors import EvaluationForbidden
    from coire_core.models.evaluation import (
        EvaluationPressureBinding,
        EvaluationWorkload,
        canonical_digest,
    )

    path = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"
    work = EvaluationWorkload.model_validate_json(path.read_bytes())
    parent = EvaluationRunRow(
        id=work.evaluation_id,
        fence=work.fence,
        subjects=[work.target.model_dump(mode="json")],
        suite_snapshot=work.suite.model_dump(mode="json"),
        started_deadline_at=work.deadline,
        execution_deadline_at=work.deadline,
        started_at=datetime.now(UTC),
        measurement_id=None,
    )
    attempt = EvaluationAttemptRow(
        id=work.attempt_id,
        run_id=parent.id,
        phase=work.phase,
        fence=work.fence,
        target_sha256=canonical_digest(work.target),
        deadline_at=work.deadline,
    )
    validate_phase_binding(parent, attempt, work)
    changed = (
        work.model_copy(update={"suite": work.suite.model_copy(update={"version": 2})})
        if change == "suite"
        else work.model_copy(update={"phase": "candidate"})
        if change == "phase"
        else work.model_copy(
            update={
                "target": work.target.model_copy(
                    update={
                        "runtime": work.target.runtime.model_copy(
                            update={"engine_version": "foreign"}
                        )
                    }
                )
            }
        )
        if change == "target"
        else work.model_copy(update={"deadline": datetime.now(UTC)})
        if change == "deadline"
        else work.model_copy(
            update={"pressure": EvaluationPressureBinding(measurement_id=uuid.uuid4())}
        )
    )
    with pytest.raises(EvaluationForbidden):
        validate_phase_binding(parent, attempt, changed)


def test_evaluation_phase_grant_is_one_exact_read_target_without_tools() -> None:
    target = InferenceTarget(
        model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
    )
    run = AgentRunRow(
        purpose="evaluation",
        task_class=TaskClass.READ,
        primary_model_id=target.model_id,
        primary_variant_id=target.variant_id,
        primary_adapter_id=None,
        evaluation_attempt_id=uuid.uuid4(),
    )
    scope = RunTokenScope(
        permitted_model_ids=frozenset({target.model_id}),
        permitted_targets=(target,),
        spend_limit_tokens=100,
    )
    validate_run_scope(run, scope)
    for changed in (
        scope.model_copy(update={"permitted_tools": frozenset({"read_file"})}),
        scope.model_copy(
            update={"permitted_model_ids": frozenset({target.model_id, uuid.uuid4()})}
        ),
        scope.model_copy(update={"permitted_targets": ()}),
    ):
        with pytest.raises(InvalidRunToken):
            validate_run_scope(run, changed)
    run.task_class = TaskClass.WRITE
    with pytest.raises(InvalidRunToken):
        validate_run_scope(run, scope)


def test_child_cannot_attach_training_inputs_from_an_unrelated_parent(tmp_path: Path) -> None:
    from evaluation_input_fixtures import staged_workload

    from coire_api.evaluation.execution import validate_phase_binding
    from coire_core.errors import EvaluationForbidden
    from coire_core.models.evaluation import canonical_digest

    work = staged_workload(tmp_path)
    parent = EvaluationRunRow(
        id=work.evaluation_id,
        fence=work.fence,
        subjects=[work.target.model_dump(mode="json")],
        suite_snapshot=work.suite.model_dump(mode="json"),
        started_deadline_at=work.deadline,
        execution_deadline_at=work.deadline,
        started_at=datetime.now(UTC),
        measurement_id=None,
    )
    attempt = EvaluationAttemptRow(
        id=work.attempt_id,
        run_id=parent.id,
        phase=work.phase,
        fence=work.fence,
        target_sha256=canonical_digest(work.target),
        deadline_at=work.deadline,
    )
    with pytest.raises(EvaluationForbidden):
        validate_phase_binding(parent, attempt, work)
